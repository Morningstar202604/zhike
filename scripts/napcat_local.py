"""NapCat 本地运行器（Shell 模式，独立于桌面 QQ 客户端）。

用法：
    python scripts/napcat_local.py start              # 首次：起 NapCat，WebUI 扫码
    python scripts/napcat_local.py start --qq 123456  # 之后：快速登录，免扫码
    python scripts/napcat_local.py status             # 探测 WebUI(6099) / OneBot WS(3001) / 登录态
    python scripts/napcat_local.py stop               # 结束本脚本启动的 NapCat

设计要点：
- NapCat Shell 是**独立设备**：首次登录必须一次扫码/确认（腾讯登录安全，
  任何独立协议端都绕不过），之后凭据持久化在 .local/napcat 下，
  `start --qq <号>` 走快速登录，永久免扫码。
- 只绑 127.0.0.1：WebUI(6099) 与 OneBot WS(3001) 都不暴露公网。
- OneBot WS 服务端配置预写在 config/onebot11.json（NapCat 对未配置账号
  会回退读全局配置并另存为账号配置——从 v4.18.41 产物源码验证）。
"""
from __future__ import annotations

import argparse
import base64
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAPCAT_DIR = ROOT / ".local" / "napcat"
# QQ 内核数据目录：必须与桌面版 QQ 隔离，否则内核卡在 initializing（见 _launch_direct 注释）
NAPCAT_DATA_DIR = ROOT / ".local" / "napcat-data"
RUN_LOG = NAPCAT_DIR / "run.log"
PID_FILE = NAPCAT_DIR / "napcat.pid"
WEBUI_PORT = int(os.environ.get("NAPCAT_WEBUI_PORT", "6099"))
WS_PORT = int(os.environ.get("ONEBOT_WS_PORT", "3001"))
WEBUI_TOKEN = "zhike-local-6099"


def _ws_handshake_ok(port: int = WS_PORT) -> tuple[bool, str]:
    """与 gateway/bot.py 的预检同一套判据：能 101 才算登录+OneBot 就绪。"""
    crlf = "\r\n"
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=3)
    except OSError as e:
        return False, f"端口 {port} 不可达（{type(e).__name__}）"
    try:
        sock.sendall(crlf.join([
            "GET / HTTP/1.1",
            f"Host: 127.0.0.1:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {base64.b64encode(secrets.token_bytes(16)).decode()}",
            "Sec-WebSocket-Version: 13",
            "", "",
        ]).encode())
        sock.settimeout(3)
        head = sock.recv(64)
        if not head:
            return False, "端口可连但无响应（未登录的典型表现）"
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " in status:
            return True, ""
        return False, f"WS 升级被拒：{status[:60]!r}"
    except OSError:
        return False, "握手超时/中断"
    finally:
        sock.close()


def _webui_up() -> bool:
    try:
        sock = socket.create_connection(("127.0.0.1", WEBUI_PORT), timeout=2)
        sock.close()
        return True
    except OSError:
        return False


def _tail(path: Path, lines: int = 15) -> list[str]:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return content[-lines:]
    except OSError:
        return []


def _qq_exe_from_registry() -> str:
    """从注册表读 QQ 安装路径（launcher .bat 用的同一判据）。"""
    import winreg

    key = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key) as k:
                uninstall, _ = winreg.QueryValueEx(k, "UninstallString")
        except OSError:
            continue
        # UninstallString = "C:\...\Uninstall.exe" → 同目录下的 QQ.exe
        exe = Path(str(uninstall).strip('"')).parent / "QQ.exe"
        if exe.exists():
            return str(exe)
    return ""


def _version_warnings() -> list[str]:
    """检查 QQ 版本是否与 NapCat 注入包匹配。

    为什么必须查：NapCat 的注入是按 QQ 内部结构打补丁的，``qqnt.json`` 里写死了
    它适配的 QQ 版本。QQ 自动升级后版本号一变，注入就静默失败——
    表现是 QQ.exe 起来了但内存只有 20MB、WebUI/OneBot 端口全不通，
    ``run.log`` 里却只有 "Process resumed" 看似正常。这是本项目实际踩过的坑
    （NapCat 适配 9.9.22-40990，机器上被升到 9.9.36-53644，QQ 群一整天没响应）。
    这里不阻断启动（有人可能就想试试），但必须把话说在前面。
    """
    warns: list[str] = []
    pkg = NAPCAT_DIR / "qqnt.json"
    try:
        import json as _json

        want = str(_json.loads(pkg.read_text(encoding="utf-8")).get("version") or "")
    except (OSError, ValueError):
        return warns
    if not want:
        return warns

    have = ""
    try:
        # 目录名即版本：.../versions/9.9.36-53644/
        exe = _qq_exe_from_registry()
        if exe:
            vdir = Path(exe).parent / "versions"
            cands = sorted((d.name for d in vdir.iterdir() if d.is_dir()), reverse=True)
            have = cands[0] if cands else ""
    except OSError:
        pass

    if have and have != want:
        warns.append(
            f"QQ 版本不匹配：NapCat 适配 {want}，本机是 {have}。"
            "注入很可能失败（QQ 进程起来但端口不通）。"
            "处理：把 QQ 降级到该版本，或换用适配当前 QQ 的 NapCat 版本。"
        )
    return warns


def _launch_direct(uin: str, log_fh) -> subprocess.Popen:
    """直接调 NapCatWinBootMain.exe 注入 QQ，绕开 launcher .bat。

    为什么要绕开：NapCat 自带的 launcher-user.bat 在结尾（以及每个失败分支）
    都写了 `pause`。人工双击运行时它只是"按任意键退出"，但在**无人值守启动**
    （本脚本、CI、服务化部署）里，没有控制台给它喂按键，进程会永久停在
    `请按任意键继续...`——表现为 NapCat 永远起不来、QQ 群一直没响应，
    而日志里只有这一行，极难定位。这里直接调底层 exe，参数与 .bat 完全一致。
    """
    exe = NAPCAT_DIR / "NapCatWinBootMain.exe"
    hook = NAPCAT_DIR / "NapCatWinBootHook.dll"
    if not exe.exists():
        raise FileNotFoundError(f"缺少 {exe.name}")
    qq = _qq_exe_from_registry()
    if not qq:
        raise FileNotFoundError("注册表里找不到 QQ 安装路径（先安装桌面版 QQ）")
    args = [str(exe), qq, str(hook)]
    if uin:
        args.append(uin)
    # 数据目录必须与桌面版 QQ 隔离。
    # QQ 内核通过注册表 HKLM\...\Tencent\QQNT Install 解析数据目录，默认落到桌面版
    # 共用的 Tencent Files。共用后内核卡在 initializing：不登录、不写日志、不开端口，
    # 二维码看着正常但扫了没反应（NapCat issue #2135，v4.18.42 起支持该变量）。
    env = dict(os.environ)
    env.setdefault("NAPCAT_QQ_DATA_DIR", str(NAPCAT_DATA_DIR))
    creationflags = 0x00000008 if os.name == "nt" else 0  # DETACHED_PROCESS
    return subprocess.Popen(args, cwd=str(NAPCAT_DIR), stdout=log_fh, stderr=log_fh,
                            env=env, creationflags=creationflags)


def cmd_start(uin: str, wait_s: int) -> int:
    if not NAPCAT_DIR.exists():
        print(f"[X] 未找到 NapCat：{NAPCAT_DIR}")
        print("    先下载 Release 的 NapCat.Shell.zip 解压到这里（见 docs/QQ-LOCAL.md）")
        return 2
    for w in _version_warnings():
        print(f"[!] {w}")
    if _ws_handshake_ok()[0]:
        print(f"[OK] OneBot WS 已就绪（{WS_PORT}），无需重复启动。")
        return 0

    log_fh = open(RUN_LOG, "ab")
    try:
        proc = _launch_direct(uin, log_fh)
        how = "NapCatWinBootMain.exe（直连，避开 .bat 的 pause）"
    except FileNotFoundError as e:
        # 兜底：直连不可用时仍试 .bat，但提示它可能卡在 pause
        print(f"[!] 直连启动不可用（{e}），回退到 launcher.bat")
        launcher = NAPCAT_DIR / ("launcher.bat" if os.name != "nt" else "launcher-user.bat")
        if not launcher.exists():
            launcher = NAPCAT_DIR / "launcher.bat"
        args = [str(launcher)] + ([uin] if uin else [])
        creationflags = 0x00000008 if os.name == "nt" else 0
        proc = subprocess.Popen(args, cwd=str(NAPCAT_DIR), stdout=log_fh, stderr=log_fh,
                                creationflags=creationflags)
        how = f"{launcher.name}（注意：内含 pause，无人值守时可能卡住）"

    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print(f"[启动] NapCat pid={proc.pid} 方式={how} 日志={RUN_LOG}")
    if uin:
        print(f"[登录] 使用快速登录：{uin}（凭据不存在时会回退扫码）")
    else:
        print(f"[登录] 首次登录：打开 http://127.0.0.1:{WEBUI_PORT}/webui ，令牌 {WEBUI_TOKEN}，扫码/确认")
        print("       扫一次即可，之后 `start --qq <QQ号>` 永久免扫码。")

    deadline = time.time() + wait_s
    qr_hinted = False
    while time.time() < deadline:
        time.sleep(3)
        ok, reason = _ws_handshake_ok()
        if ok:
            print(f"[OK] OneBot WS 已就绪（127.0.0.1:{WS_PORT}）—— QQ 已登录。")
            return 0
        if not qr_hinted and _webui_up():
            print(f"[等待] WebUI 已就绪：http://127.0.0.1:{WEBUI_PORT}/webui （令牌 {WEBUI_TOKEN}）")
            qr_hinted = True
        if proc.poll() is not None:
            print(f"[X] NapCat 进程退出（code={proc.returncode}），日志末尾：")
            for line in _tail(RUN_LOG, 12):
                print("   ", line[:160])
            return 1
    print(f"[!] {wait_s}s 内 OneBot 未就绪（{reason}）。")
    print("    未登录：浏览器打开 WebUI 扫码；已登录但未起 WS：检查 config/onebot11_*.json")
    for line in _tail(RUN_LOG, 8):
        print("   ", line[:160])
    return 1


def cmd_status() -> int:
    webui = _webui_up()
    ok, reason = _ws_handshake_ok()
    print(f"WebUI(6099): {'up' if webui else 'down'}")
    print(f"OneBot WS(3001): {'ready' if ok else 'not ready'}" + ("" if ok else f" — {reason}"))
    pid = PID_FILE.read_text(encoding="utf-8").strip() if PID_FILE.exists() else ""
    if pid:
        # pid 只记录 launcher；真正干活的是它派生的 QQ/NapCat 子进程，
        # launcher 退出不代表服务停了——一切以 3001 握手结果为准。
        print(f"napcat.pid={pid}（launcher 记录；服务状态以 OneBot WS 为准）")
    for w in _version_warnings():
        print(f"[!] {w}")
    return 0 if ok else 1


def cmd_stop() -> int:
    if not PID_FILE.exists():
        print("[i] 没有 pid 记录。")
        return 0
    pid = PID_FILE.read_text(encoding="utf-8").strip()
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
    else:
        subprocess.run(["kill", pid], capture_output=True)
    PID_FILE.unlink(missing_ok=True)
    print(f"[OK] 已结束 NapCat（pid={pid}）。")
    return 0


def main() -> int:
    # 避免中文输出在 GBK 控制台变乱码（launcher 子进程输出同样可能带 UTF-8）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_start = sub.add_parser("start", help="启动 NapCat（首次扫码，之后 --qq 快速登录）")
    p_start.add_argument("--qq", default="", help="QQ 号：有凭据时免扫码快速登录")
    p_start.add_argument("--wait", type=int, default=120, help="等待登录的秒数（默认 120）")
    sub.add_parser("status", help="查看 WebUI / OneBot WS / 登录态")
    sub.add_parser("stop", help="停止本脚本启动的 NapCat")
    args = ap.parse_args()
    if args.cmd == "start":
        return cmd_start(args.qq, args.wait)
    if args.cmd == "status":
        return cmd_status()
    return cmd_stop()


if __name__ == "__main__":
    sys.exit(main())
