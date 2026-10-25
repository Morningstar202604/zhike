"""本轮修复的回归用例：每一条都对应一个已复现的真实缺陷。

命名规则：test_<缺陷关键词>_<期望行为>，注释写清「修复前会怎样」。
"""
import asyncio
import json

import httpx
import pytest

from core import http_pool, llm, reminders, store
from core.actions import service as actions_service


# ---------------- 429 短冷却（NameError） ----------------

def test_rate_limited_uses_configured_short_cooldown(monkeypatch):
    """修复前：_RATE_LIMIT_COOLDOWN_S 未定义，429 分支抛 NameError 打断整条链。"""
    assert isinstance(llm._RATE_LIMIT_COOLDOWN_S, float)

    calls: list[str] = []

    async def fake_post(base, key, payload):
        calls.append(payload["model"])
        if payload["model"] == "m1":
            raise llm.RateLimited(0.0)
        return "ok:" + payload["model"]

    monkeypatch.setattr(llm, "_post_chat", fake_post)
    monkeypatch.setattr(llm, "_HEDGE_S", 0.01)
    llm._COOLDOWN.clear()
    try:
        cfg = {"api_key": "k", "base_url": "https://a.cn/v1", "model": "m1",
               "fallback_model": "m2", "providers": []}
        parsed = asyncio.run(llm._race_chain(llm._chain(cfg, "m1"), [], {},
                                             parse=lambda t: {"a": t}))
        assert parsed == {"a": "ok:m2"}, "429 后必须换到备胎，而不是整条链断掉"
        # 进了短冷却池（不是普通 120s 冷却），本次取值为 5s 上限内
        assert llm._cooling("https://a.cn/v1", "m1")
    finally:
        llm._COOLDOWN.clear()


# ---------------- 提醒：时区比较 ----------------

def test_reminder_due_compares_real_instants(monkeypatch, tmp_path):
    """修复前：ISO 字符串直接比较，+08:00 的已过期提醒会被判成未到期。"""
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "r.db"))
    store._SCHEMA_READY.clear()
    store.init_db()
    rec = store.reminder_add(
        visitor_id="v1", chat_key="webchat:v1", text="喝水",
        due_at="2026-10-25T09:00:00+08:00", channel_type="webchat",
    )
    # 09:00+08:00 == 01:00Z；01:30Z 时已经过期
    due = store.reminder_due("2026-10-25T01:30:00+00:00")
    assert [r["id"] for r in due] == [rec["id"]]
    # 未到期（00:30Z）不应取出
    assert store.reminder_due("2026-10-25T00:30:00+00:00") == []


def test_reminder_claim_is_atomic(monkeypatch, tmp_path):
    """修复前：只 SELECT 不认领，多 worker 会把同一条提醒推送多次。"""
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "r2.db"))
    store._SCHEMA_READY.clear()
    store.init_db()
    rec = store.reminder_add(visitor_id="v", chat_key="webchat:v", text="x",
                             due_at="2020-01-01T00:00:00+00:00")
    assert store.reminder_claim(rec["id"]) is True
    assert store.reminder_claim(rec["id"]) is False
    store.reminder_release(rec["id"])
    assert store.reminder_claim(rec["id"]) is True


def test_web_reminder_uses_visitor_latest_session(monkeypatch, tmp_path):
    """修复前：把 chat_key 里的 visitor_id 当 session_id 查，网页提醒永远投递失败。"""
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "r3.db"))
    store._SCHEMA_READY.clear()
    store.init_db()
    store.create_session("real-sid", "visitor-1")

    rec = {"id": 1, "text": "提醒", "chat_key": "webchat:visitor-1",
           "channel_type": "webchat", "visitor_id": "visitor-1",
           "due_at": "2026-10-25T09:00:00+08:00"}
    ok, err = asyncio.run(reminders._deliver_web(rec, "提醒内容"))
    assert ok, err
    msgs = store.get_messages("real-sid")
    assert any("提醒内容" in (m.get("content") or "") for m in msgs)


def test_reminder_tick_skips_already_claimed(monkeypatch, tmp_path):
    """tick 必须先认领：已被别的进程抢占的行不能再投递。"""
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "r4.db"))
    store._SCHEMA_READY.clear()
    store.init_db()
    rec = store.reminder_add(visitor_id="v", chat_key="webchat:v", text="x",
                             due_at="2020-01-01T00:00:00+00:00")
    assert store.reminder_claim(rec["id"]) is True
    delivered: list[int] = []

    async def fake_deliver(row):
        delivered.append(row["id"])
        return True

    monkeypatch.setattr(reminders, "deliver", fake_deliver)
    assert asyncio.run(reminders.tick()) == 0
    assert delivered == []


# ---------------- 动作：幂等与审批 ----------------

def _l2_payload(**extra):
    return {"action": "set_group_kick",
            "params": {"group_id": "1", "user_id": "2"},
            "principal": "admin", **extra}


def test_idempotency_key_does_not_block_approved_execution(client, monkeypatch):
    """修复前：带 idempotency_key 的 L2 动作，批准后仍返回旧的 pending_approval，永不执行。"""
    monkeypatch.setenv("ACTION_L2_ENABLED", "true")
    monkeypatch.setattr(actions_service, "_run_action", _fake_run_action)
    payload = _l2_payload(idempotency_key="run-42")
    first = client.post("/api/actions/execute", json=payload).json()
    assert first["status"] == "pending_approval"
    ap_id = first["approval_id"]
    assert client.post(f"/api/approvals/{ap_id}/approve").status_code == 200
    second = client.post("/api/actions/execute", json={**payload, "approval_id": ap_id})
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "executed", "批准后必须真正执行"


def test_approval_digest_matches_raw_params_not_masked(client, monkeypatch):
    """修复前：用打码后的参数比对，带密钥的写入批准后永远对不上（或可被替换）。"""
    monkeypatch.setenv("ACTION_L2_ENABLED", "true")
    monkeypatch.setattr(actions_service, "_run_action", _fake_run_action)
    payload = {"action": "set_group_kick",
               "params": {"group_id": "1", "user_id": "sk-abcdefgh12345678"},
               "principal": "admin"}
    first = client.post("/api/actions/execute", json=payload).json()
    ap_id = first["approval_id"]
    assert client.post(f"/api/approvals/{ap_id}/approve").status_code == 200
    r = client.post("/api/actions/execute", json={**payload, "approval_id": ap_id})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "executed"


def test_approval_rejects_changed_params(client, monkeypatch):
    monkeypatch.setenv("ACTION_L2_ENABLED", "true")
    monkeypatch.setattr(actions_service, "_run_action", _fake_run_action)
    payload = {"action": "set_group_kick",
               "params": {"group_id": "1", "user_id": "2"}, "principal": "admin"}
    ap_id = client.post("/api/actions/execute", json=payload).json()["approval_id"]
    client.post(f"/api/approvals/{ap_id}/approve")
    r = client.post("/api/actions/execute", json={
        **payload, "params": {"group_id": "1", "user_id": "3"}, "approval_id": ap_id})
    assert r.status_code == 403


async def _fake_run_action(spec, params, principal_label, agent_autonomous_tier, session_id):
    return {"ok": True}


# ---------------- HTTP 连接池：timeout 必须参与分桶 ----------------

def test_http_pool_separates_clients_by_timeout():
    """修复前：同 base_url 复用 client，5s 健康检查会把 120s 长任务锁成 5s。"""
    short = http_pool.get_client("https://timeout.example", timeout=5)
    long = http_pool.get_client("https://timeout.example", timeout=120)
    assert short is not long
    assert short.timeout.read != long.timeout.read


# ---------------- 检索：索引故障必须退 bigram ----------------

def test_retrieval_falls_back_when_index_raises(monkeypatch):
    from core import retrieval

    kb = [{"id": "KB1", "title": "退货", "answer": "七天无理由退货", "tags": []}]
    monkeypatch.setattr(retrieval, "load_kb", lambda: kb)
    monkeypatch.setattr(retrieval, "_ensure_conn", lambda: object())

    def boom(conn, entries):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(retrieval, "rebuild_index_if_needed", boom)
    hits = retrieval.retrieve_scored("退货", 3, 0.0)
    assert hits and hits[0][1]["id"] == "KB1", "索引故障应退回 bigram，而不是抛 500"


# ---------------- 会话 TTL：旧会话必须收尾 ----------------

def test_expired_channel_session_is_closed(monkeypatch, tmp_path):
    """修复前：TTL 过期只覆盖映射，旧会话永久停在 ongoing。"""
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "s.db"))
    store._SCHEMA_READY.clear()
    store.init_db()
    from core import sessions

    old_sid = sessions.resolve_session("qq_group:1", "v1")
    # 人为把映射时间推到 TTL 之外
    with store.get_conn() as conn:
        conn.execute("UPDATE channel_sessions SET updated_at = '2020-01-01T00:00:00+00:00' "
                     "WHERE channel = 'qq_group:1'")
    new_sid = sessions.resolve_session("qq_group:1", "v1")
    assert new_sid != old_sid
    old = store.get_session(old_sid)
    assert old["status"] == "closed", "换新会话时旧会话必须收尾"
    assert store.get_session(new_sid)["status"] == "ongoing"


# ---------------- 工具调用：**kwargs 透传 ----------------

@pytest.mark.asyncio
async def test_invoke_passes_args_to_var_keyword_tool():
    """修复前：按参数名过滤，`def run(**kw)` 永远收到空参数。"""
    from core import tools as tools_mod

    captured = {}

    def impl(**kw):
        captured.update(kw)
        return {"tool": "kb_search", "connected": True, "found": True, "note": "ok"}

    tools_mod._SYNC_IMPLS["__kw_probe"] = impl
    try:
        res = await tools_mod.invoke("__kw_probe", {"topic": "退货", "top_k": "3"})
    finally:
        tools_mod._SYNC_IMPLS.pop("__kw_probe", None)
    assert captured == {"topic": "退货", "top_k": "3"}
    assert res["note"] == "ok"


# ---------------- Agent harness：OpenAI function calling ----------------

def test_available_tools_include_json_schema():
    from core import agent_loop
    from core.actions import authz

    tools = agent_loop.available_tools(authz.parse_principal("admin"))
    spec = next(t for t in tools if t["name"] == "get_group_info")
    assert spec["parameters"]["type"] == "object"
    assert "group_id" in spec["parameters"]["properties"]
    assert spec["parameters"]["required"] == ["group_id"]


def test_to_openai_tools_shape():
    from core import agent_loop

    tools = [{"name": "x", "description": "d", "parameters": {"type": "object", "properties": {}}}]
    converted = agent_loop.to_openai_tools(tools)
    assert converted == [{"type": "function", "function": {
        "name": "x", "description": "d",
        "parameters": {"type": "object", "properties": {}},
    }}]


def test_agent_llm_parses_native_tool_calls(monkeypatch):
    """官方 function calling 响应必须能解析成内部决策，并保留 tool_call_id。"""
    from core import agent_llm

    monkeypatch.setattr(agent_llm, "_creds", lambda: ("k", "https://a.cn/v1", "m"))

    async def fake_post(base, key, payload, timeout_s):
        assert payload["tools"][0]["type"] == "function", "必须走官方 tools 协议"
        return httpx.Response(200, json={
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {"content": None, "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "fs.read", "arguments": '{"path": "/work/a"}'},
                }]},
            }],
            "usage": {"total_tokens": 42},
        }, request=httpx.Request("POST", "https://a.cn/v1/chat/completions"))

    monkeypatch.setattr(agent_llm, "_post", fake_post)
    decision = asyncio.run(agent_llm.call_llm(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        [{"name": "fs.read", "description": "读", "parameters": {"type": "object", "properties": {}}}],
    ))
    assert decision["tool"] == "fs.read"
    assert decision["args"] == {"path": "/work/a"}
    assert decision["tool_call_id"] == "call_1"
    assert decision["usage"]["total_tokens"] == 42


def test_agent_llm_falls_back_when_tools_unsupported(monkeypatch):
    """端点不认 tools 时要自动退回 JSON 模式，而不是整轮失败。"""
    from core import agent_llm

    monkeypatch.setattr(agent_llm, "_creds", lambda: ("k", "https://a.cn/v1", "m"))
    seen: list[dict] = []

    async def fake_post(base, key, payload, timeout_s):
        seen.append(payload)
        if "tools" in payload:
            return httpx.Response(400, json={"error": "tools unsupported"},
                                  request=httpx.Request("POST", "https://a.cn/v1/chat/completions"))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({"tool": None, "final": "好"})}}],
        }, request=httpx.Request("POST", "https://a.cn/v1/chat/completions"))

    monkeypatch.setattr(agent_llm, "_post", fake_post)
    decision = asyncio.run(agent_llm.call_llm(
        [{"role": "user", "content": "u"}],
        [{"name": "fs.read", "description": "读", "parameters": {"type": "object", "properties": {}}}],
    ))
    assert decision["final"] == "好"
    assert len(seen) == 2 and "tools" in seen[0] and "tools" not in seen[1]


def test_agent_llm_trims_history():
    from core import agent_llm

    msgs = [{"role": "system", "content": "s"}] + [
        {"role": "user", "content": f"m{i}"} for i in range(100)]
    trimmed = agent_llm._trim_messages(msgs)
    assert trimmed[0]["role"] == "system"
    assert len(trimmed) <= agent_llm.MAX_HISTORY_MESSAGES + 1
