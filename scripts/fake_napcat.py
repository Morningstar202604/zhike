"""伪装 NapCat（OneBot v11 WebSocket 服务端），实测 网关→core→回复 全链路。

用法：core 运行在 :8000 后执行  .venv/Scripts/python scripts/fake_napcat.py
自动拉起 gateway/bot.py 子进程，模拟群友 @机器人 发问，捕获机器人的 send_group_msg 回复。
"""
import asyncio
import json
import os
import subprocess
import sys
import time

import websockets

SELF_ID = 1000
GROUP_ID = 88123
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
received_actions: list[dict] = []


async def handler(ws, path=None):
    print(f"[fake-napcat] 网关已连接 path={path or getattr(getattr(ws, 'request', None), 'path', '?')}", flush=True)
    await ws.send(json.dumps({
        "time": int(time.time()), "self_id": SELF_ID, "post_type": "meta_event",
        "meta_event_type": "lifecycle", "sub_type": "connect",
    }))

    async def probe():
        await asyncio.sleep(2.5)
        await ws.send(json.dumps({
            "time": int(time.time()), "self_id": SELF_ID, "post_type": "message",
            "message_type": "group", "group_id": GROUP_ID, "user_id": 456,
            "sub_type": "normal", "message_id": 12345, "font": 0,
            "message": [
                {"type": "at", "data": {"qq": str(SELF_ID)}},
                {"type": "text", "data": {"text": " 我的订单什么时候发货"}},
            ],
            "raw_message": f"[CQ:at,qq={SELF_ID}] 我的订单什么时候发货",
            "sender": {"nickname": "群友小测", "card": "", "role": "member"},
        }))
        print("[fake-napcat] 已注入群聊 @机器人 消息", flush=True)

    asyncio.get_running_loop().create_task(probe())
    async for raw in ws:
        data = json.loads(raw)
        action = data.get("action")
        if action is None:
            continue
        payload = None
        if action == "get_login_info":
            payload = {"user_id": SELF_ID, "nickname": "FakeBot"}
        elif action.startswith("send"):
            received_actions.append(data)
            payload = {"message_id": 999}
        elif action == "get_version_info":
            payload = {"app_name": "fake-napcat", "app_version": "0.0.1", "protocol_version": "v11"}
        await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": payload, "echo": data.get("echo")}))
        print(f"[fake-napcat] action={action} params={json.dumps(data.get('params', {}), ensure_ascii=False)[:160]}", flush=True)


async def main():
    log = open(os.path.join(ROOT, "gateway_e2e.log"), "w", encoding="utf-8")
    env = os.environ | {
        "ONEBOT_WS_URL": os.environ.get("ONEBOT_WS_URL", "ws://127.0.0.1:3001"),
        "CORE_BASE_URL": os.environ.get("CORE_BASE_URL", "http://127.0.0.1:8000"),
        "PORT": "8090",
    }
    proc = subprocess.Popen([sys.executable, "gateway/bot.py"], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        async with websockets.serve(handler, "127.0.0.1", 3001):
            print("[fake-napcat] 监听 ws://127.0.0.1:3001（伪装 NapCat 协议端）", flush=True)
            for _ in range(30):
                await asyncio.sleep(1)
                if received_actions:
                    break
        if received_actions:
            msg = received_actions[0].get("params", {}).get("message")
            print("\nCHAIN_RESULT: PASS")
            print("机器人回复:", json.dumps(msg, ensure_ascii=False)[:300])
            return 0
        print("\nCHAIN_RESULT: FAIL — 30 秒内未收到网关回复，详见 gateway_e2e.log")
        return 1
    finally:
        proc.terminate()
        log.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
