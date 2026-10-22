"""QQ 渠道全链路演练：假 NapCat（OneBot v11 正向 WS）→ NoneBot2 网关 → 核心 /v1/ingest → 回复动作校验。

覆盖四条链路：
1. 私聊文本 → 知识库命中回复（send_private_msg 含答案）
2. 群聊 @机器人 + 图片 → 无视觉模型时转人工（send_group_msg）
3. 私聊「转人工」 → 升级话术
4. 逐动作 echo ack（OneBot v11 动作协议）

用法：.venv/bin/python scripts/qq_e2e.py   （真机 QQ 登录验收清单见 DEPLOY.md §QQ 渠道）
"""
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
SELF_ID = 10000


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# 运行时分配空闲端口，避免与残留进程/其他会话冲突
CORE_PORT, NAPCAT_PORT, IMG_PORT = _free_port(), _free_port(), _free_port()
CORE = f"http://127.0.0.1:{CORE_PORT}"
SELF_ID = 10000

# 1x1 PNG（合法图片头，走魔数识别）
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _event(i: int, msg_segments: list, raw: str, *, group: int | None = None) -> dict:
    ev = {
        "time": int(time.time()),
        "self_id": SELF_ID,
        "post_type": "message",
        "sub_type": "friend" if group is None else "normal",
        "message_type": "private" if group is None else "group",
        "message_id": i,
        "user_id": 88001,
        "message": msg_segments,
        "raw_message": raw,
        "font": 0,
        "sender": {"user_id": 88001, "nickname": "演练员"},
    }
    if group is not None:
        ev["group_id"] = group
        ev["anonymous"] = None
    return ev


SCENARIO = [
    # OneBot v11 正向 WS：适配器收到 lifecycle connect 才注册 Bot，首帧必须是它
    ("lifecycle 连接元事件", {
        "time": int(time.time()),
        "self_id": SELF_ID,
        "post_type": "meta_event",
        "meta_event_type": "lifecycle",
        "sub_type": "connect",
    }),
    ("私聊 KB 问答", _event(1, [{"type": "text", "data": {"text": "退货政策是什么"}}], "退货政策是什么")),
    ("群聊 @+图片", _event(
        2,
        [{"type": "at", "data": {"qq": str(SELF_ID)}},
         {"type": "image", "data": {"url": f"http://127.0.0.1:{IMG_PORT}/img.png"}}],
        f"[CQ:at,qq={SELF_ID}][CQ:image,url=http://127.0.0.1:{IMG_PORT}/img.png]",
        group=20250101,
    )),
    ("私聊转人工", _event(3, [{"type": "text", "data": {"text": "转人工"}}], "转人工")),
]


class _ImgHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PNG)))
        self.end_headers()
        self.wfile.write(PNG)

    def log_message(self, *a):  # 静默
        pass


async def napcat_server(actions: list[dict | None]):
    """假 NapCat：lifecycle 注册 → 逐个下发业务事件 → 等网关动作并回 echo ack。"""

    async def handler(ws):
        print("[napcat] 网关已反向连接", flush=True)
        # 首帧必须是 lifecycle connect，适配器据此注册 Bot；元事件无回复，不等 ack
        for name, ev in SCENARIO:
            await ws.send(json.dumps(ev, ensure_ascii=False))
            print(f"[napcat] 已下发事件：{name}", flush=True)
            if ev.get("post_type") == "meta_event":
                continue
            got = None
            try:
                deadline = time.time() + 40
                while time.time() < deadline:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.time()))
                    print(f"[napcat] 收到帧：{str(raw)[:140]}", flush=True)
                    data = json.loads(raw)
                    if data.get("action"):
                        await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 1}, "echo": data.get("echo")}))
                        got = data
                        break
            except (asyncio.TimeoutError, websockets.ConnectionClosed) as e:
                print(f"[napcat] 事件「{name}」等待动作失败：{type(e).__name__}", flush=True)
            actions.append(got)
        print("[napcat] 场景完毕", flush=True)

    async with websockets.serve(handler, "127.0.0.1", NAPCAT_PORT, ping_interval=None):
        while len([a for a in actions if a is not None]) < 3:
            await asyncio.sleep(0.2)


def _msg_text(action: dict | None) -> str:
    if not action:
        return ""
    msg = (action.get("params") or {}).get("message")
    if isinstance(msg, str):
        return msg
    return "".join(str(seg.get("data", {}).get("text", "")) for seg in msg if isinstance(seg, dict))


async def main() -> int:
    tmp = tempfile.mkdtemp(prefix="zhike_qq_e2e_")
    env = {**os.environ, "CS_AGENT_DB": os.path.join(tmp, "e2e.db")}
    env.pop("USER_LLM_API_KEY", None)  # 规则模式，行为确定性
    # CHANNEL_TOKEN：core 现在强制要求渠道令牌（未设置则 /v1/ingest 返回 503）。
    # 演练里统一设一个固定值，core 与 gateway 共用，保证链路能打通。
    env["CHANNEL_TOKEN"] = "qq-e2e-channel-token"
    env.setdefault("ADMIN_TOKEN", "qq-e2e-admin-token")
    procs: list[subprocess.Popen] = []
    actions: list[dict | None] = []
    try:
        # 1. 本地图片源
        srv = HTTPServer(("127.0.0.1", IMG_PORT), _ImgHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        # 2. 核心（日志落盘便于排障）
        core_log = open("/tmp/zhike_qq_core.log", "w")
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "core.main:app", "--host", "127.0.0.1", "--port", str(CORE_PORT)],
            cwd=ROOT, env=env, stdout=core_log, stderr=subprocess.STDOUT,
        ))
        for _ in range(50):
            try:
                if (await httpx.AsyncClient().get(f"{CORE}/healthz")).status_code == 200:
                    break
            except Exception:
                await asyncio.sleep(0.3)
        else:
            print("FAIL: 核心服务未就绪")
            return 1
        # 3. 网关（连假 NapCat；日志落盘便于排障；PORT=nonebot 内置 uvicorn 端口，避开 8080 冲突）
        gw_env = {**env, "ONEBOT_WS_URL": f"ws://127.0.0.1:{NAPCAT_PORT}", "CORE_BASE_URL": CORE, "PORT": "8031",
                  # 演练里网关先于假协议端起进程，跳过启动前探测
                  "GATEWAY_SKIP_PREFLIGHT": "1"}
        gw_log = open("/tmp/zhike_qq_gw.log", "w")
        procs.append(subprocess.Popen(
            [sys.executable, "gateway/bot.py"], cwd=ROOT, env=gw_env,
            stdout=gw_log, stderr=subprocess.STDOUT,
        ))
        # 4. 演练
        await asyncio.wait_for(napcat_server(actions), timeout=150)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    def _is_reply(a: dict | None, msg_type: str) -> bool:
        """判断是否为指定类型的回复。

        适配器层现在用更精确的 OneBot API：
        send_private_msg / send_group_msg（旧的 matcher.send 走 send_msg）。
        两种都接受，避免依赖具体实现细节。
        """
        if not a:
            return False
        action = a.get("action")
        params = a.get("params") or {}
        if action == "send_msg":
            return params.get("message_type") == msg_type
        if msg_type == "private":
            return action == "send_private_msg"
        if msg_type == "group":
            return action == "send_group_msg"
        return False

    # 排障：把三条实际回复打印出来，断言失败时能直接看到内容
    for i, a in enumerate(actions):
        print(f"  [{i}] {_msg_text(a)[:80] if a else '(无动作)'}")
    checks = [
        ("私聊 KB 问答", _is_reply(actions[0], "private") and "退货" in _msg_text(actions[0])),
        ("群聊 @+图片→转人工", _is_reply(actions[1], "group") and "转接人工" in _msg_text(actions[1])),
        ("私聊转人工→升级话术", _is_reply(actions[2], "private") and "转接" in _msg_text(actions[2])),
    ]
    print()
    ok = True
    for name, passed in checks:
        print(f"  {'✅' if passed else '❌'} {name}")
        ok &= bool(passed)
    print("\n结果：QQ 渠道全链路" + ("全部通过 ✅" if ok else "存在失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
