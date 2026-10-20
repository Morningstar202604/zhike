"""伪装 NapCat + 验证「core → gateway → 协议端」动作全链路。

与 scripts/fake_napcat.py 的区别：那个脚本只验证「消息进来 → 回复出去」，
本脚本额外验证反向路径——core 通过 gateway 的 /internal/action 调用 OneBot API。

用法：
    python scripts/action_e2e.py [--admin-token X] [--channel-token Y]

流程：
1. 起一个假 OneBot 协议端（WS 服务端），实现 get_login_info / get_group_list / send_msg
2. 起 gateway（连假协议端，并暴露 /internal/action）
3. 起 core（指向 gateway 的 action 地址）
4. 通过 core 的 /api/actions/execute 依次验证：只读动作、写动作、拒绝路径、L2 审批
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
SELF_ID = 10001


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


CORE_PORT, GW_PORT, NAPCAT_PORT = _free_port(), _free_port(), _free_port()

ACTIONS: list[dict] = []


async def napcat_server(access_token: str = ""):
    """假协议端：对已知 action 返回固定数据，并记录收到的调用。

    传入 access_token 时校验 Authorization: Bearer，验证网关能正确透传令牌
    （NapCat 开启 access token 后，不带令牌会握手 403——这是真实会踩的坑）。
    """

    async def handler(ws):
        if access_token:
            auth = ""
            if hasattr(ws, "request") and ws.request is not None:
                auth = ws.request.headers.get("authorization", "")
            if auth != f"Bearer {access_token}":
                await ws.close(code=1008, reason="bad token")
                return
        await ws.send(json.dumps({
            "time": int(time.time()), "self_id": SELF_ID,
            "post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect",
        }))
        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception:
                continue
            action = data.get("action")
            if not action:
                continue
            ACTIONS.append({"action": action, "params": data.get("params") or {}})
            if action == "get_login_info":
                payload = {"user_id": SELF_ID, "nickname": "演练机器人"}
            elif action == "get_group_list":
                payload = [{"group_id": 100, "group_name": "测试群"}, {"group_id": 200, "group_name": "二群"}]
            elif action == "get_group_member_list":
                payload = [{"user_id": 1, "nickname": "成员A"}, {"user_id": 2, "nickname": "成员B"}]
            elif action.startswith("send"):
                payload = {"message_id": 4242}
            elif action == "set_group_kick":
                payload = None
            else:
                payload = {}
            await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": payload, "echo": data.get("echo")}))

    async with websockets.serve(handler, "127.0.0.1", NAPCAT_PORT, ping_interval=None):
        while True:
            await asyncio.sleep(1)


def _wait_ok(url: str, headers: dict, tries: int = 60) -> bool:
    for _ in range(tries):
        try:
            r = httpx.get(url, headers=headers, timeout=3)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _start_napcat_thread(access_token: str) -> threading.Thread:
    """在独立线程里跑假协议端的 WS 服务（主线程要用同步 httpx 做断言）。"""

    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(napcat_server(access_token))

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    return t


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin-token", default="action-e2e-admin")
    ap.add_argument("--channel-token", default="action-e2e-chan")
    ap.add_argument("--onebot-token", default="action-e2e-onebot",
                    help="模拟 NapCat 开启 access token；留空则不启用")
    args = ap.parse_args()

    core = f"http://127.0.0.1:{CORE_PORT}"
    admin_h = {"X-Admin-Token": args.admin_token}
    tmp = tempfile.mkdtemp(prefix="zhike_act_")
    env = {
        **os.environ,
        "CS_AGENT_DB": os.path.join(tmp, "act.db"),
        "CS_AGENT_KB": os.path.join(tmp, "kb.json"),
        "CS_AGENT_RETRIEVAL_DB": os.path.join(tmp, "idx.db"),
        "ADMIN_TOKEN": args.admin_token,
        "CHANNEL_TOKEN": args.channel_token,
        "ACTION_L2_ENABLED": "true",
        "AGENT_TIER": "2",
        "GATEWAY_ACTION_URL": f"http://127.0.0.1:{GW_PORT}",
        "ONEBOT_WS_URL": f"ws://127.0.0.1:{NAPCAT_PORT}",
        "ONEBOT_ACCESS_TOKEN": args.onebot_token,
        "CORE_BASE_URL": core,
        "PORT": str(GW_PORT),
    }
    env.pop("USER_LLM_API_KEY", None)

    procs: list[subprocess.Popen] = []
    results: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, cond, detail))

    try:
        gw_log = open(os.path.join(tmp, "gateway.log"), "w", encoding="utf-8")
        core_log = open(os.path.join(tmp, "core.log"), "w", encoding="utf-8")

        # core 先起（gateway 会向它上报心跳，但这不是必须的）
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "core.main:app", "--host", "127.0.0.1", "--port", str(CORE_PORT)],
            cwd=ROOT, env=env, stdout=core_log, stderr=subprocess.STDOUT,
        ))
        if not _wait_ok(f"{core}/healthz", {}):
            print("FAIL: core 未就绪")
            return 1

        # 假协议端 + gateway（协议端开启 access token，验证网关能透传）
        _start_napcat_thread(args.onebot_token)
        time.sleep(1)
        procs.append(subprocess.Popen(
            [sys.executable, "gateway/bot.py"], cwd=ROOT, env=env,
            stdout=gw_log, stderr=subprocess.STDOUT,
        ))
        # 等 gateway 的内部端点可用
        gw_ready = False
        for _ in range(60):
            try:
                r = httpx.get(f"http://127.0.0.1:{GW_PORT}/internal/status",
                              headers={"X-Channel-Token": args.channel_token}, timeout=3)
                if r.status_code == 200:
                    gw_ready = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        check("gateway 内部端点可用", gw_ready, f"port={GW_PORT}")
        if not gw_ready:
            return _report(results)

        # 等协议端连上（bot 注册）
        connected = False
        for _ in range(40):
            try:
                r = httpx.get(f"http://127.0.0.1:{GW_PORT}/internal/status",
                              headers={"X-Channel-Token": args.channel_token}, timeout=3).json()
                if r.get("connected"):
                    connected = True
                    check("协议端已连接", True, f"self_id={r.get('self_id')}")
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not connected:
            check("协议端已连接", False, "bot 未注册")

        def exec_action(payload: dict) -> httpx.Response:
            return httpx.post(f"{core}/api/actions/execute", json=payload, headers=admin_h, timeout=30)

        # 1) 只读动作
        r = exec_action({"action": "get_login_info", "params": {}, "principal": "admin"})
        ok = r.status_code == 200 and r.json().get("status") == "executed"
        nick = r.json().get("result", {}).get("nickname") if ok else ""
        check("只读动作 get_login_info", ok and nick == "演练机器人", f"{r.status_code} {nick}")

        # 2) 只读列表
        r = exec_action({"action": "get_group_list", "params": {}, "principal": "admin"})
        groups = r.json().get("result") if r.status_code == 200 else None
        check("只读动作 get_group_list", isinstance(groups, list) and len(groups) == 2,
              f"{len(groups) if isinstance(groups, list) else 'n/a'} 个群")

        # 3) 写动作（可逆）
        r = exec_action({"action": "send_group_msg",
                         "params": {"group_id": 100, "message": "测试消息"}, "principal": "admin"})
        check("可逆写 send_group_msg", r.status_code == 200 and r.json().get("status") == "executed",
              str(r.json().get("result"))[:60])

        # 4) 普通成员被拒
        r = exec_action({"action": "send_group_msg",
                         "params": {"group_id": 100, "message": "越权"}, "principal": "qq:55555"})
        check("普通成员写操作被拒", r.status_code == 403, f"{r.status_code}")

        # 5) L2 进入审批
        kick = {"action": "set_group_kick", "params": {"group_id": 100, "user_id": 2}, "principal": "admin"}
        r = exec_action(kick)
        body = r.json() if r.status_code == 200 else {}
        ap_id = body.get("approval_id")
        check("L2 动作进入待审批", body.get("status") == "pending_approval" and bool(ap_id), str(ap_id))

        # 6) 未审批执行被拒
        r = exec_action({**kick, "approval_id": ap_id})
        check("未审批不得执行", r.status_code == 409, f"{r.status_code}")

        # 7) 批准后执行
        if ap_id:
            httpx.post(f"{core}/api/approvals/{ap_id}/approve", headers=admin_h, timeout=10)
            r = exec_action({**kick, "approval_id": ap_id})
            check("批准后执行成功", r.status_code == 200 and r.json().get("status") == "executed",
                  f"{r.status_code}")

        # 8) 审计链完整
        r = httpx.get(f"{core}/api/admin/audit/verify", headers=admin_h, timeout=10)
        check("审计链完整", r.status_code == 200 and r.json().get("ok") is True,
              str(r.json().get("checked")) + " 条")

        # 9) 动作确实到达协议端
        reached = any(a["action"] == "get_login_info" for a in ACTIONS)
        check("动作真实到达协议端", reached, f"{len(ACTIONS)} 次调用")

        # 10) 参数不透传多余字段
        r = exec_action({"action": "get_group_list", "params": {"evil": "x"}, "principal": "admin"})
        dropped = r.json().get("dropped_params") if r.status_code == 200 else None
        check("多余参数被丢弃", dropped == ["evil"], str(dropped))

    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    return _report(results)


def _report(results) -> int:
    width = max((len(n) for n, _, _ in results), default=10)
    failed = 0
    for name, ok, detail in results:
        failed += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
    print(f"\n合计 {len(results)} 项：通过 {len(results) - failed}，失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
