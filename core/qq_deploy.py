"""QQ 协议端（NapCat Shell）部署编排：下载 → 解压 → 启动 → 状态。

设计立场（用户明确要求）：个人秘书 Agent 本身不依赖 QQ——QQ 是**可选渠道**。
所以协议端的安装/启动必须能从控制台自助完成，而不是让用户去克隆别的东西。

部署目录固定 ROOT/.local/napcat（scripts/napcat_local.py 的启动路径读这里），
解压时跳过 config/ 以保留用户配置（webui.json / onebot11*.json / 登录凭据）。

下载用 core.napcat_fetch（并行分段严格校验）；同一时间只允许一个部署任务。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

logger = logging.getLogger("cs.events")

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = Path(os.environ.get("NAPCAT_DEPLOY_DIR") or ROOT / ".local" / "napcat")
VERSION_FILE = DEPLOY_DIR / "version.txt"
RUN_LOG = ROOT / ".local" / "logs" / "napcat-deploy.log"

_lock = threading.Lock()
state: dict = {"stage": "idle", "pct": 0, "note": "", "version": ""}


def _set(stage: str, pct: int = 0, note: str = "") -> None:
    state.update(stage=stage, pct=pct, note=note)
    logger.info("qq deploy: %s %s %s", stage, pct, note[:80])


def deploy_status() -> dict:
    """给控制台的状态卡：装没装 / 跑没跑 / 部署进度。"""
    installed = (DEPLOY_DIR / "NapCatWinBootMain.exe").exists()
    version = ""
    try:
        version = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return {
        "installed": installed,
        "version": version,
        "running_3001": _port_up(3001),
        "running_6099": _port_up(6099),
        "stage": state["stage"],
        "pct": state["pct"],
        "note": state["note"],
    }


def _port_up(port: int) -> bool:
    import socket

    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=1.5)
        s.close()
        return True
    except OSError:
        return False


def latest_release() -> tuple[str, str, int]:
    """(tag, 直链, 字节数)。代理与直连都试。"""
    import httpx

    api = "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest"
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
        or "http://127.0.0.1:7897"
    last_err: Exception | None = None
    for client_kwargs in ({"proxy": proxy}, {}):
        try:
            with httpx.Client(timeout=30, **client_kwargs) as c:  # type: ignore[arg-type]
                rel = c.get(api).json()
                tag = rel["tag_name"]
                asset = next(a for a in rel["assets"] if a["name"] == "NapCat.Shell.zip")
                return tag, asset["browser_download_url"], asset["size"]
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"拿不到最新版本信息：{type(last_err).__name__}: {last_err}")


def deploy() -> str:
    """启动后台部署线程。返回给前端的提示。"""
    busy_stages = ("checking", "downloading", "extracting")
    if _lock.locked() or state.get("stage") in busy_stages:
        return "部署已在进行中（看进度条）。"
    threading.Thread(target=_deploy_worker, daemon=True).start()
    return "部署已开始——进度条见下方，完成后点「启动协议端」。"


def _extract_with_retry(zip_path: Path, dest: Path, tries: int = 5) -> int:
    """打开并解压；Windows 上杀软可能短暂锁住新文件，小退避重试。"""
    last: Exception | None = None
    for i in range(tries):
        try:
            with zipfile.ZipFile(zip_path) as zf:
                n = 0
                for info in zf.infolist():
                    if info.filename.startswith("config/"):
                        continue  # 用户配置与登录凭据不覆盖
                    zf.extract(info, dest)
                    n += 1
            return n
        except PermissionError as e:
            last = e
            time.sleep(0.5 * (i + 1))
    raise last if last else RuntimeError("extract failed")


def _deploy_worker() -> None:
    from core.napcat_fetch import strict_download

    with _lock:
        try:
            _set("checking", 1, "查询最新版本…")
            tag, url, size = latest_release()
            _set("downloading", 2, f"下载 {tag}（{size // 1048576} MB）…")

            def progress(done: int) -> None:
                _set("downloading", max(2, min(90, int(done * 88 / size))),
                     f"下载 {tag}… {done * 100 // size}%")

            tmp = DEPLOY_DIR / "_shell.zip"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            strict_download(url, size, str(tmp), jobs=6, progress=progress, cwd=str(DEPLOY_DIR))
            _set("extracting", 92, "校验并解压（保留已有配置）…")
            n = _extract_with_retry(tmp, DEPLOY_DIR)
            VERSION_FILE.write_text(tag, encoding="utf-8")
            tmp.unlink(missing_ok=True)
            _set("done", 100, f"OK 已部署 {tag}（{n} 文件）——点「启动协议端」")
            logger.info("qq deploy done: %s (%d files)", tag, n)
        except Exception as e:  # noqa: BLE001 - 失败要如实显示在前端
            _set("error", 0, f"部署失败：{type(e).__name__}: {str(e)[:140]}")
            logger.warning("qq deploy failed: %s", e)


def protocol_start(qq: str) -> str:
    """后台启动 Shell（scripts/napcat_local.py 负责引导注入与等待）。"""
    exe = DEPLOY_DIR / "NapCatWinBootMain.exe"
    if not exe.exists():
        return "X 协议端还没部署——先点「一键部署」。"
    script = ROOT / "scripts" / "napcat_local.py"
    if not script.exists():
        return "X 缺少 scripts/napcat_local.py"
    log_fh = open(ROOT / ".local" / "logs" / "napcat-start.log", "ab")
    subprocess.Popen(
        [sys.executable, str(script), "start", "--qq", qq],
        cwd=str(ROOT), stdout=log_fh, stderr=log_fh,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return ("OK 协议端启动中（后台，约 2 分钟）。已登录过的号会自动登录；"
            "首次登录请回到「扫码登录」页扫码。")
