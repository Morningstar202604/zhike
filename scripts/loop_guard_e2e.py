"""E2E 验证：网关侧的乒乓循环护栏是否真的拦住（不是只跑单测）。

拓扑：假 NapCat(WS:3099) -> 被测 gateway(8091) -> 假 core(HTTP:8095)
不碰真实 QQ、不碰真实 core、不调模型。

对面（100000003）连续 6 条道别语，每条都引用我们上一条回复的 message_id：
预期前 3 条有回复、第 4 条起被判 closing_loop 静默（不再发 send_group_msg），
且 gateway 日志出现 loop guard muted。

用法：.venv/Scripts/python.exe scripts/loop_guard_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import websockets
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
SELF_ID = 100000004
PEER = 100000003
GROUP = 100000002
WS_PORT = 3099
CORE_PORT = 8095
GW_PORT = 8091

sent: list[dict] = []
last_sent_id = {"v": 7000}


class FakeCore(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        body = json.dumps({"reply": {"text": "行，真睡了，晚安喵～", "mode": "llm_react",
                                     "citations": [], "confidence": 1.0, "escalate": False,
                                     "escalate_reason": "", "tool_used": None,
                                     "tool_result": None, "thought": ""}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        body = b'{"online": true}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


async def napcat(ws):
    await ws.send(json.dumps({"time": int(time.time()), "self_id": SELF_ID,
                              "post_type": "meta_event", "meta_event_type": "lifecycle",
                              "sub_type": "connect"}))
    task = asyncio.create_task(reader(ws))
    try:
        for i in range(6):
            await asyncio.sleep(1.6)
            mid = 900000 + i
            msg = [
                {"type": "reply", "data": {"id": str(last_sent_id["v"])}},
                {"type": "at", "data": {"qq": str(SELF_ID)}},
                {"type": "text", "data": {"text": f" 晚安，第{i + 1}次了，真睡了。"}},
            ]
            await ws.send(json.dumps({
                "time": int(time.time()), "self_id": SELF_ID, "post_type": "message",
                "message_type": "group", "group_id": GROUP, "user_id": PEER,
                "sub_type": "normal", "message_id": mid, "font": 0, "message": msg,
                "raw_message": "", "to_me": True,
                "sender": {"nickname": "零&雪/彼岸双生", "card": "", "role": "member"},
            }))
            print(f"[test] 注入第 {i + 1} 条道别语（引用 {last_sent_id['v']}）", flush=True)
        await asyncio.sleep(6)
    finally:
        task.cancel()


async def reader(ws):
    async for raw in ws:
        data = json.loads(raw)
        action = data.get("action")
        if action == "send_group_msg":
            seg = str((data.get("params") or {}).get("message") or "")
            sent.append({"n": len(sent) + 1, "seg": seg})
            last_sent_id["v"] = 8000 + len(sent)
            print(f"[test] 网关发出第 {len(sent)} 条回复", flush=True)
        payload = {"message_id": last_sent_id["v"]} if action and action.startswith("send") else None
        if action == "get_login_info":
            payload = {"user_id": SELF_ID, "nickname": "零"}
        await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": payload,
                                  "echo": data.get("echo")}))


async def main() -> int:
    srv = HTTPServer(("127.0.0.1", CORE_PORT), FakeCore)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    workdir = pathlib.Path(tempfile.mkdtemp(prefix="loopguard-e2e-"))
    state = workdir / "e2e_loop_guard.json"
    log_path = workdir / "e2e_gateway.log"
    log = log_path.open("w", encoding="utf-8")
    env = {**os.environ,
           "ONEBOT_WS_URL": f"ws://127.0.0.1:{WS_PORT}",
           "CORE_BASE_URL": f"http://127.0.0.1:{CORE_PORT}",
           "PORT": str(GW_PORT), "HOST": "127.0.0.1",
           "GATEWAY_SKIP_PREFLIGHT": "1",
           "GATEWAY_LOOP_STATE": str(state),
           "GATEWAY_LOOP_GUARD": "1",
           "GATEWAY_GROUP_MIN_GAP": "0",
           "GATEWAY_GROUP_HOURLY_CAP": "1000",
           "GATEWAY_SPLIT_CHARS": "9000",
           "GATEWAY_TYPING_CPS": "1000",
           "GATEWAY_DELAY_CAP": "0",
           "GATEWAY_SPLIT_GAP": "0",
           "ADMIN_TOKEN": "e2e-admin", "CHANNEL_TOKEN": "e2e-channel"}
    proc = subprocess.Popen([sys.executable, "gateway/bot.py"], cwd=str(ROOT), env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        async with websockets.serve(napcat, "127.0.0.1", WS_PORT):
            await asyncio.sleep(16)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        srv.shutdown()

    text = log_path.read_text(encoding="utf-8", errors="replace")
    muted = [ln for ln in text.splitlines() if "loop guard muted" in ln]
    ok = len(sent) == 3 and len(muted) >= 1
    if ok:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"\n[result] 网关回复条数={len(sent)}，loop guard 命中={len(muted)}")
    for ln in muted:
        print("  ", ln.strip()[:160])
    print("E2E_RESULT:", "PASS" if ok else "FAIL")
    if not ok:
        print("---- gateway log tail ----")
        print("\n".join(text.splitlines()[-25:]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
