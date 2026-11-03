"""QQ 接入路径自检：告诉你现在这台机器该走哪条路。

设计动机：QQ 接入有四种失败方式，现象全都长得一样——「群里 @ 了没反应」。
逐个人工排查要翻注册表、看进程内存、试端口握手、比对版本号，一次半小时起步。
本脚本把这些判据固化下来，一条命令给出结论和下一步动作。

用法：
    python scripts/qq_doctor.py
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAPCAT_DIR = ROOT / ".local" / "napcat"
WS_PORT = int(os.environ.get("ONEBOT_WS_PORT", "3001"))
WEBUI_PORT = int(os.environ.get("NAPCAT_WEBUI_PORT", "6099"))

OK = "[OK]  "
WARN = "[!]   "
BAD = "[X]   "
INFO = "[i]   "


def _ws_ready(port: int = WS_PORT) -> tuple[bool, str]:
    """与网关预检同一判据：能拿到 101 才算协议端真就绪。"""
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
    except OSError as e:
        return False, f"端口不可达（{type(e).__name__}）"
    try:
        import base64
        import secrets

        req = "\r\n".join([
            "GET / HTTP/1.1", f"Host: 127.0.0.1:{port}",
            "Upgrade: websocket", "Connection: Upgrade",
            f"Sec-WebSocket-Key: {base64.b64encode(secrets.token_bytes(16)).decode()}",
            "Sec-WebSocket-Version: 13", "", "",
        ]).encode()
        sock.sendall(req)
        sock.settimeout(2)
        head = sock.recv(64)
        if not head:
            return False, "端口可连但握手无响应（未登录的典型表现）"
        line = head.split(b"\r\n", 1)[0]
        return (True, "") if b" 101 " in line else (False, f"升级被拒：{line[:50]!r}")
    except OSError:
        return False, "握手超时"
    finally:
        sock.close()


def _port_open(port: int) -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=2)
        s.close()
        return True
    except OSError:
        return False


def _qq_processes() -> list[tuple[int, int]]:
    """返回 [(pid, 内存MB)]。只认 Windows。"""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='QQ.exe'", "get", "ProcessId,WorkingSetSize"],
            capture_output=True, text=True, errors="replace", timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        nums = line.split()
        if len(nums) == 2 and all(n.isdigit() for n in nums):
            rows.append((int(nums[0]), int(nums[1]) // 1024 // 1024))
    return rows


def _version_hint() -> tuple[str, str]:
    """读 NapCat 适配的 QQ 版本与本机 QQ 版本（从注册表/目录名）。"""
    want = ""
    pkg = NAPCAT_DIR / "qqnt.json"
    if pkg.is_file():
        try:
            want = str(json.loads(pkg.read_text(encoding="utf-8")).get("version") or "")
        except (OSError, ValueError):
            pass
    have = ""
    if os.name == "nt":
        try:
            import winreg

            key = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                uninstall, _ = winreg.QueryValueEx(k, "UninstallString")
            vdir = Path(str(uninstall).strip('"')).parent / "versions"
            names = sorted((d.name for d in vdir.iterdir() if d.is_dir()), reverse=True)
            have = names[0] if names else ""
        except OSError:
            pass
    return want, have


def _docker_state() -> tuple[bool, bool, str]:
    """返回 (有 CLI, 引擎在跑, 说明)。"""
    cli = shutil.which("docker") is not None
    if not cli:
        return False, False, "未安装 docker 命令"
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, text=True,
                           errors="replace", timeout=20)
        if r.returncode == 0:
            return True, True, "引擎正常"
        return True, False, "CLI 存在但引擎未运行（Docker Desktop 没起 / WSL 未就绪）"
    except (OSError, subprocess.SubprocessError) as e:
        return True, False, f"探测失败：{type(e).__name__}"


def _wsl_state() -> str:
    if os.name != "nt":
        return "非 Windows"
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Windows-Subsystem-Linux).State"],
            capture_output=True, text=True, errors="replace", timeout=60,
        )
        state = r.stdout.strip().splitlines()[-1].strip() if r.stdout.strip() else "未知"
        return state
    except (OSError, subprocess.SubprocessError):
        return "未知"


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    print("=" * 62)
    print("QQ 接入自检")
    print("=" * 62)

    ws_ok, ws_reason = _ws_ready()
    webui = _port_open(WEBUI_PORT)
    procs = _qq_processes()
    want, have = _version_hint()
    has_cli, engine_ok, docker_note = _docker_state()
    wsl = _wsl_state()

    # ---- 1. 协议端 ----
    print("\n[1] 协议端（NapCat / OneBot WS）")
    if ws_ok:
        print(f"{OK}OneBot WS({WS_PORT}) 已就绪——QQ 已登录，可以启动网关")
    else:
        print(f"{BAD}OneBot WS({WS_PORT}) 不可用：{ws_reason}")
    print(f"{INFO}WebUI({WEBUI_PORT})：{'监听中' if webui else '未监听'}")

    # ---- 2. QQ 进程健康度 ----
    print("\n[2] QQ 进程")
    if not procs:
        print(f"{INFO}没有 QQ 进程在跑")
    else:
        for pid, mb in procs:
            # 正常 QQ 主进程几百 MB；注入失败时只有约 20MB，内核根本没起来
            flag = f"{WARN}疑似僵死（注入失败）" if mb < 80 else f"{OK}正常"
            print(f"{flag}pid={pid} 内存={mb}MB")
        if any(mb < 80 for _, mb in procs):
            print(f"{INFO}20MB 级别的 QQ 进程 = 内核没启动。协议端端口必然不通。")

    # ---- 3. 版本匹配 ----
    print("\n[3] 版本匹配")
    if want and have:
        if want == have:
            print(f"{OK}NapCat 适配 {want}，本机 {have}，一致")
        else:
            print(f"{BAD}不匹配：NapCat 适配 {want}，本机 {have}")
            print(f"{INFO}QQ 自动升级会让注入静默失败。这是本机注入路径最常见的死因。")
    elif want:
        print(f"{INFO}NapCat 适配 {want}；本机 QQ 版本未取到")
    else:
        print(f"{INFO}未找到 .local/napcat/qqnt.json")

    # ---- 4. Docker 可行性 ----
    print("\n[4] Docker 路线（推荐，容器自带 QQ，不受本机版本影响）")
    if not has_cli:
        print(f"{BAD}没有 docker 命令")
    elif engine_ok:
        print(f"{OK}Docker 引擎可用")
        print(f"{INFO}下一步：docker compose -f docs/napcat-compose.yaml up -d")
        print(f"{INFO}然后开 http://127.0.0.1:{WEBUI_PORT}/webui 扫码")
    else:
        print(f"{BAD}{docker_note}")
        print(f"{INFO}WSL 功能状态：{wsl}")
        if wsl.lower() == "disabled":
            print(f"{INFO}需先启用 WSL 与 VirtualMachinePlatform，然后重启系统。")

    # ---- 5. 结论 ----
    print("\n" + "=" * 62)
    print("结论")
    print("=" * 62)
    if ws_ok:
        print("协议端已就绪。启动网关即可：python gateway/bot.py")
        print("验证：curl -H \"X-Admin-Token: $ADMIN\" localhost:8010/api/gateway/health")
        return 0
    if engine_ok:
        print("走 Docker：容器自带 QQ，绕开本机版本不匹配。")
        print("  docker compose -f docs/napcat-compose.yaml up -d")
        return 1
    if want and have and want != have:
        print("本机注入不可用（版本不匹配）。两条路选一：")
        print("  A) 装 Docker，用容器版（推荐，不会再被 QQ 升级影响）")
        print("  B) 把 QQ 降到 " + want + " 并关闭自动更新")
        return 1
    print("协议端未就绪，且未发现明确的版本冲突。")
    print("  先看 .local/napcat/logs/ 与 .local/napcat/run.log")
    print("  或直接用 Docker：docker compose -f docs/napcat-compose.yaml up -d")
    return 1


if __name__ == "__main__":
    sys.exit(main())
