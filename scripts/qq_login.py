"""QQ 登录助手：把 NapCat 的二维码直接打到终端，并跟踪登录状态。

用法：
    python scripts/qq_login.py                 # 默认容器名 zk-napcat
    python scripts/qq_login.py --container zk-napcat --timeout 300

它做三件事：
1. 从容器里取出当前二维码 PNG，用 ANSI 色块在终端画出（手机直接扫屏幕即可）
2. 打印 NapCat 给的「二维码解码URL」作为兜底（终端画歪了就用它）
3. 每 3 秒轮询日志与 3001 端口，登录成功/失败都给出明确结论

为什么要这个脚本：
NapCat 的二维码每 2 分钟刷新一次，且只存在容器内 `/app/napcat/cache/qrcode.png`。
如果手动 docker cp + 找文件，等拿到图可能已经过期。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

QR_IN_CONTAINER = "/app/napcat/cache/qrcode.png"


def run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, f"{type(e).__name__}: {e}"


def draw_qr(path: str) -> bool:
    """把二维码画成终端色块。返回是否成功。"""
    try:
        from PIL import Image
    except ImportError:
        print("（未安装 Pillow，跳过终端二维码；直接用下面的图片路径/解码链接）")
        return False
    try:
        im = Image.open(path).convert("L")
    except Exception as e:
        print(f"（读取二维码失败：{e}）")
        return False

    # 统一缩放到 ~41x41 的字符网格：终端里 2 个半角空格≈1 个方块宽高比
    target = 41
    im = im.resize((target, target), Image.NEAREST)
    px = im.load()
    # 每个字符用 "██"（深）/ "  "（浅），并留 1 格静区
    print()
    print("  " + "  " * target)
    for y in range(target):
        row = "  "
        for x in range(target):
            row += "  " if px[x, y] > 128 else "██"
        print(row)
    print("  " + "  " * target)
    print()
    return True


def latest_qr_url(logs: str) -> str:
    hits = re.findall(r"(https://txz\.qq\.com/p\?k=[^\s]+)", logs)
    return hits[-1] if hits else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="zk-napcat")
    ap.add_argument("--timeout", type=int, default=300, help="等待扫码的秒数")
    ap.add_argument("--out", default="", help="二维码保存路径（默认系统临时目录）")
    args = ap.parse_args()

    if shutil.which("docker") is None:
        print("找不到 docker，请先安装并启动 Docker Desktop。")
        return 2

    code, out = run(["docker", "inspect", "-f", "{{.State.Running}}", args.container])
    if code != 0 or "true" not in out.lower():
        print(f"容器 {args.container} 不在运行。先执行：")
        print(f"  docker start {args.container}")
        print("或首次创建：见 docs/QQ-LOGIN.md")
        return 2

    out_path = args.out or os.path.join(os.environ.get("TEMP", "/tmp"), "zhike-qq-qr.png")
    code, out = run(["docker", "cp", f"{args.container}:{QR_IN_CONTAINER}", out_path])
    if code != 0:
        print("取二维码失败（QQ 可能还没开始登录流程，等 10 秒再试）：")
        print(out.strip()[:300])
        return 1

    _, logs = run(["docker", "logs", "--tail", "80", args.container])
    url = latest_qr_url(logs)

    print("=" * 62)
    print(" 用手机 QQ 扫描下方二维码（或在手机上打开解码链接）")
    print(f" 图片文件：{out_path}")
    if url:
        print(f" 解码链接：{url}")
    print("=" * 62)
    draw_qr(out_path)

    print("开始等待登录（每 3 秒检查一次）…")
    deadline = time.time() + args.timeout
    last_note = ""
    while time.time() < deadline:
        time.sleep(3)
        _, logs = run(["docker", "logs", "--tail", "40", args.container])
        if re.search(r"(登录成功|账号已上线|Online|上线成功|扫码成功)", logs):
            print("\n[OK] 登录成功。")
            break
        if re.search(r"(二维码已失效|登录失败|扫码失败|expired)", logs):
            print("\n[!] 二维码已失效，正在取新码…")
            run(["docker", "cp", f"{args.container}:{QR_IN_CONTAINER}", out_path])
            draw_qr(out_path)
            continue
        # 端口探测：登录成功后 NapCat 才会监听 OneBot WS
        probe = ("import socket,sys;"
                 "s=socket.socket(); s.settimeout(1.5);"
                 "sys.exit(0 if s.connect_ex(('127.0.0.1',3001))==0 else 1)")
        rc, _ = run(["docker", "exec", args.container, "python3", "-c", probe])
        note = "3001 已监听" if rc == 0 else "3001 未监听（未登录时不开放，属正常）"
        if note != last_note:
            print(f"  · {note}")
            last_note = note
    else:
        print("\n[超时] 尚未检测到登录成功。可重新运行本脚本获取最新二维码。")
        return 1

    # 登录后再确认一次端口
    time.sleep(3)
    rc, _ = run(["docker", "exec", args.container, "python3", "-c",
                 "import socket,sys;s=socket.socket();s.settimeout(1.5);"
                 "sys.exit(0 if s.connect_ex(('127.0.0.1',3001))==0 else 1)"])
    if rc == 0:
        print("OneBot WS 已就绪：ws://127.0.0.1:3001")
        print("下一步：启动网关 → python gateway/bot.py（见 docs/QQ-LOGIN.md）")
        return 0
    print("登录成功但 3001 未监听：检查 NapCat「网络配置」里 WS 服务端是否启用（端口 3001）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
