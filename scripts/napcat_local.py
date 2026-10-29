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


def cmd_start(uin: str, wait_s: int) -> int:
    if not NAPCAT_DIR.exists():
        print(f"[X] 未找到 NapCat：{NAPCAT_DIR}")
        print("    先下载 Release 的 NapCat.Shell.zip 解压到这里（见 docs/QQ-LOCAL.md）")
        return 2
    if _ws_handshake_ok()[0]:
        print(f"[OK] OneBot WS 已就绪（{WS_PORT}），无需重复启动。")
        return 0

    launcher = NAPCAT_DIR / ("launcher.bat" if os.name != "nt" else "launcher-user.bat")
    if not launcher.exists():
        launcher = NAPCAT_DIR / "launcher.bat"
    args = [str(launcher)]
    if uin:
        args.append(uin)

    log_fh = open(RUN_LOG, "ab")
    creationflags = 0x00000008 if os.name == "nt" else 0  # DETACHED_PROCESS
    proc = subprocess.Popen(args, cwd=str(NAPCAT_DIR), stdout=log_fh, stderr=log_fh,
                            creationflags=creationflags)
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print(f"[启动] NapCat pid={proc.pid} 日志={RUN_LOG}")
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
