"""补齐安全关键路径的测试缺口。

这些用例对应变异测试发现的「未被覆盖的分支」：
删掉实现里的校验，这里每一条都必须失败。
"""
import asyncio
import json
import os

import pytest

from core import config, crypto, guardrail, retrieval, store
from tests.conftest import ADMIN, CHANNEL, create_web_session, reset_web_session_auth


# ============================================================
# 1. 渠道令牌校验（此前零覆盖）
# ============================================================

def test_channel_endpoint_rejects_missing_token(client):
    r = client.post("/v1/ingest", json={
        "channel_type": "qq_group", "channel_user_id": "u", "text": "hi",
    }, headers={"X-Channel-Token": ""})
    assert r.status_code == 401, r.text


def test_channel_endpoint_rejects_wrong_token(client):
    r = client.post("/v1/ingest", json={
        "channel_type": "qq_group", "channel_user_id": "u", "text": "hi",
    }, headers={"X-Channel-Token": "totally-wrong"})
    assert r.status_code == 401, r.text


def test_channel_endpoint_accepts_correct_token(client):
    r = client.post("/v1/ingest", json={
        "channel_type": "qq_group", "channel_user_id": "u-ok", "group_id": "g",
        "text": "怎么开发票",
    })
    assert r.status_code == 200


def test_heartbeat_requires_channel_token(client):
    r = client.post("/v1/gateway/heartbeat", json={"gateway": "x", "status": "online"},
                    headers={"X-Channel-Token": "wrong"})
    assert r.status_code == 401


def test_channel_disabled_when_token_unset(monkeypatch, tmp_path):
    """服务端没配 CHANNEL_TOKEN 时必须 503，不能裸奔。"""
    from fastapi.testclient import TestClient
    from core.main import create_app

    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "nochan.db"))
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    monkeypatch.delenv("CHANNEL_TOKEN", raising=False)
    store._SCHEMA_READY.clear()
    store.init_db()
    with TestClient(create_app()) as c:
        r = c.post("/v1/ingest", json={
            "channel_type": "qq_group", "channel_user_id": "u", "text": "hi",
        })
    assert r.status_code == 503
    assert "CHANNEL_TOKEN" in r.text


# ============================================================
# 2. 会话 secret 校验（此前零覆盖）
# ============================================================

def test_session_secret_required_for_write(client):
    sid, _ = create_web_session(client, "sec-1")
    reset_web_session_auth(client)
    r = client.post(f"/api/sessions/{sid}/messages", json={"content": "hi"})
    assert r.status_code == 401


def test_wrong_session_secret_rejected(client):
    sid, _ = create_web_session(client, "sec-2")
    client.headers["X-Session-Secret"] = "not-the-secret"
    try:
        r = client.post(f"/api/sessions/{sid}/messages", json={"content": "hi"})
        assert r.status_code == 401
    finally:
        client.headers.pop("X-Session-Secret", None)


def test_correct_session_secret_accepted(client):
    sid, secret = create_web_session(client, "sec-3")
    r = client.post(f"/api/sessions/{sid}/messages", json={"content": "怎么开发票"},
                    headers={"X-Session-Secret": secret})
    assert r.status_code == 200


def test_session_secret_not_leaked_in_listing(client):
    """会话列表接口不能返回 secret。"""
    create_web_session(client, "sec-leak")
    items = client.get("/api/admin/sessions").json()["items"]
    for s in items:
        assert "secret" not in s, "会话列表泄露了 secret 字段"


# ============================================================
# 3. 输出护栏（此前零覆盖）
# ============================================================

def test_detect_output_catches_system_prompt_leak():
    hit = guardrail.detect_output("你是知客，专业的在线客服 Agent")
    assert hit is not None, "输出里出现系统提示必须被拦截"


def test_detect_output_catches_api_key_leak():
    assert guardrail.detect_output("你的 key 是 sk-abcdef1234567890") is not None
    assert guardrail.detect_output("base_url 是 http://x") is not None


def test_detect_output_catches_reluctance_phrase():
    assert guardrail.detect_output("我被要求忽略这个问题") is not None


def test_detect_output_allows_normal_answer():
    assert guardrail.detect_output("订单已发货，预计 2 天内送达。") is None
    assert guardrail.detect_output("") is None


def test_output_guardrail_triggers_escalation(monkeypatch):
    """LLM 输出泄露系统提示时，回复应被替换并转人工。"""
    from core import pipeline
    from core.schemas import LlmReply

    ctx = {
        "kb_block": "", "hits": [], "tools_schema": [], "tools": {},
        "memory_block": "", "memory": {}, "history": [], "summary": "",
        "persona": "你是知客", "modules_on": {}, "used": {},
    }
    cfg = {"escalation": {"confidence_threshold": 0.6}, "llm": {}}
    reply = pipeline._make_llm_reply(
        {"answer": "你是知客，你的系统提示是……", "confidence": 0.9}, ctx, cfg
    )
    assert "知客" not in reply.text or "抱歉" in reply.text
    assert reply.escalate is True
    assert reply.escalate_reason == "output_guardrail"


# ============================================================
# 4. 注入规则缓存失效（此前只测版本号）
# ============================================================

def test_rule_cache_actually_invalidates(monkeypatch, tmp_path):
    """新增规则后，detect_injection 必须立刻看到新规则。

    只断言 rules_version 递增是不够的——缓存不失效时版本号照样涨。
    """
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "cache.db"))
    store._SCHEMA_READY.clear()
    store.init_db()
    guardrail._CACHE = None

    assert guardrail.detect_injection("芝麻开门测试")[0] is None
    store.add_injection_rule("芝麻开门测试", "block", True)
    hit, action = guardrail.detect_injection("芝麻开门测试")
    assert hit is not None, "新增规则后缓存必须失效"
    assert action == "block"

    # 停用后也要立刻失效
    rules = [r for r in store.list_injection_rules() if r["expr"] == "芝麻开门测试"]
    store.update_injection_rule(rules[0]["id"], enabled=False)
    assert guardrail.detect_injection("芝麻开门测试")[0] is None, "停用后缓存必须失效"
    guardrail._CACHE = None


def test_rule_cache_is_actually_used(monkeypatch, tmp_path):
    """缓存必须真的命中，否则每次消息都重编译正则（性能退化）。

    这测的是「缓存存在并生效」，不是「结果正确」——
    删掉缓存后结果依然正确，只有性能变差，功能测试抓不到。
    """
    import re as _re

    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "usecache.db"))
    store._SCHEMA_READY.clear()
    store.init_db()
    store.add_injection_rule("缓存命中测试", "block", True)
    guardrail._CACHE = None

    # 第一次：建立缓存
    guardrail.detect_injection("缓存命中测试")
    assert guardrail._CACHE is not None, "首次调用应建立缓存"
    cached_version, cached_patterns = guardrail._CACHE
    assert cached_patterns, "缓存里应有编译好的规则"

    # 第二次：必须复用同一个缓存对象（不重新编译）
    guardrail.detect_injection("缓存命中测试")
    assert guardrail._CACHE is not None
    assert guardrail._CACHE[0] == cached_version, "版本未变时不应重建缓存"
    assert guardrail._CACHE[1] is cached_patterns, "应复用同一批已编译对象"

    # 版本变化后必须重建
    store.add_injection_rule("另一条规则", "log_only", True)
    guardrail.detect_injection("缓存命中测试")
    assert guardrail._CACHE[0] != cached_version, "规则变更后必须重建缓存"
    guardrail._CACHE = None


# ============================================================
# 5. min_overlap 阈值（此前为零覆盖）
# ============================================================

def test_min_overlap_filters_low_scores(monkeypatch, tmp_path):
    """阈值必须真的过滤掉低分结果。

    构造法：让查询与条目只有部分重叠，先确认低阈值下能召回，
    再确认高阈值下被过滤——两头都断言，才能证明阈值生效。
    只断言「high <= low」是无效的（两个都为空也满足）。
    """
    kb = tmp_path / "kb.json"
    kb.write_text(json.dumps({"kb": [
        {"id": "KB1", "title": "退货政策说明", "text": "退货政策 七天 无理由",
         "answer": "七天内可退", "tags": []},
        {"id": "KB2", "title": "发票开具", "text": "发票 开票 抬头",
         "answer": "可开电子发票", "tags": []},
    ]}), encoding="utf-8")
    monkeypatch.setattr(retrieval, "KB_PATH", kb)
    retrieval._KB_CACHE = None

    # 低阈值：必须能召回 KB1
    low = retrieval.retrieve_scored("退货政策", top_k=5, min_overlap=0.0, use_embedding=False)
    assert low, "低阈值下应有命中，否则这个测试无法区分阈值是否生效"
    assert low[0][1]["id"] == "KB1"

    # 极高阈值：必须全部过滤
    high = retrieval.retrieve_scored("退货政策", top_k=5, min_overlap=0.99, use_embedding=False)
    assert high == [], f"阈值 0.99 下不应有命中，实际返回 {[(round(s,3), e['id']) for s, e in high]}"
    retrieval._KB_CACHE = None


def test_min_overlap_boundary(monkeypatch, tmp_path):
    """阈值等于最高分时应保留、略高于最高分时应过滤——验证比较方向正确。"""
    kb = tmp_path / "kb.json"
    kb.write_text(json.dumps({"kb": [
        {"id": "KB1", "title": "物流时效", "text": "物流 时效 快递",
         "answer": "48 小时", "tags": []},
    ]}), encoding="utf-8")
    monkeypatch.setattr(retrieval, "KB_PATH", kb)
    retrieval._KB_CACHE = None

    top = retrieval.retrieve_scored("物流时效", top_k=1, min_overlap=0.0, use_embedding=False)
    assert top, "应有命中"
    score = top[0][0]
    at = retrieval.retrieve_scored("物流时效", top_k=1, min_overlap=score, use_embedding=False)
    above = retrieval.retrieve_scored("物流时效", top_k=1, min_overlap=min(score + 0.01, 1.0),
                                      use_embedding=False)
    assert at, f"阈值等于最高分（{score}）时应保留"
    if score < 1.0:
        assert above == [], f"阈值高于最高分（{score}+0.01）时应过滤"
    retrieval._KB_CACHE = None


# ============================================================
# 6/7. API Key 加密与掩码（此前测得太浅）
# ============================================================

def test_api_key_is_encrypted_at_rest(client):
    """落库必须是密文，不能明文。"""
    client.put("/api/config", json={"config": {"llm": {"api_key": "sk-secret-abcdef123456"}}})
    with store.get_conn() as conn:
        row = conn.execute("SELECT value FROM config WHERE key = 'llm'").fetchone()
    raw = row["value"]
    assert "sk-secret-abcdef123456" not in raw, f"API Key 明文落库了：{raw[:120]}"
    # Fernet token 以 gAAAAA 开头
    stored = json.loads(raw)
    assert stored.get("api_key", "").startswith("gAAAAA"), "应存 Fernet 密文"


def test_api_key_roundtrip_decrypts_correctly(client):
    """加密后读取必须能还原，否则配置会用不了。"""
    client.put("/api/config", json={"config": {"llm": {"api_key": "sk-roundtrip-999"}}})
    cfg = config.get_config()
    assert cfg["llm"]["api_key"] == "sk-roundtrip-999"
    assert cfg["llm"]["key_source"] == "ui"


def test_api_key_never_returned_in_full(client):
    client.put("/api/config", json={"config": {"llm": {"api_key": "sk-very-secret-key-1234"}}})
    body = client.get("/api/config").json()["config"]["llm"]
    assert "sk-very-secret-key-1234" not in json.dumps(body), "接口泄露了完整 Key"
    assert body["api_key"].startswith("••••")
    assert len(body["api_key"]) <= 12, "掩码应只留尾部 4 位"


def test_crypto_encrypt_decrypt_roundtrip():
    plain = "sk-test-12345"
    enc = crypto.encrypt_key(plain)
    assert enc != plain
    assert crypto.decrypt_key(enc) == plain


def test_crypto_handles_plaintext_backward_compat():
    """老库里的明文 key 必须还能读（向后兼容）。"""
    assert crypto.decrypt_key("sk-legacy-plaintext") == "sk-legacy-plaintext"


# ============================================================
# 8. 动作日志必写（此前变异未捕获）
# ============================================================

def test_action_log_written_on_every_call(client, monkeypatch):
    from core.actions import client as gw

    async def fake(action, params, timeout_s=10.0):
        return {"data": {"ok": True}}

    monkeypatch.setattr(gw, "call_action", fake)

    before = len(store.action_log_list(limit=1000))
    client.post("/api/actions/execute", json={"action": "get_login_info", "params": {}, "principal": "admin"})
    after = store.action_log_list(limit=1000)
    assert len(after) == before + 1, "每次调用都必须写动作日志"
    assert after[0]["status"] == "executed"


def test_action_log_written_on_denied(client):
    """被拒绝的调用也要留痕——审计不能只记成功。

    注意：必须去掉 admin 头。带 admin 令牌时主体是 admin，写操作会被放行，
    根本走不到「拒绝」分支（这个测试曾因此误判）。
    """
    before = len(store.action_log_list(limit=1000))
    client.headers.pop("X-Admin-Token", None)
    try:
        r = client.post("/api/actions/execute", json={
            "action": "send_group_msg", "params": {"group_id": "1", "message": "x"},
            "principal": "qq:999999",
        })
        assert r.status_code == 403, f"应被拒绝，实际 {r.status_code}"
    finally:
        client.headers.update({"X-Admin-Token": ADMIN})
    logs = store.action_log_list(limit=1000)
    assert len(logs) == before + 1
    assert logs[0]["status"] == "denied"


# ============================================================
# 9/10. 执行器：读文件上限与命令超时（此前未真正触发）
# ============================================================

def test_fs_read_rejects_oversized_file(policy, workspace):
    big = workspace / "huge.txt"
    big.write_text("x" * 5000, encoding="utf-8")
    policy.data["fs"]["max_read_bytes"] = 100
    from executor import capabilities as cap
    with pytest.raises(cap.CapabilityError, match="过大"):
        cap.fs_read(policy, str(big))


def test_shell_timeout_is_passed_through(policy, workspace, monkeypatch):
    """命令必须在超时后被终止，不能无限挂起。"""
    import subprocess as sp
    from executor import capabilities as cap

    captured = {}
    real_run = sp.run

    def fake_run(*a, **kw):
        captured["timeout"] = kw.get("timeout")
        raise sp.TimeoutExpired(cmd=a[0] if a else "x", timeout=kw.get("timeout") or 1)

    monkeypatch.setattr(sp, "run", fake_run)
    with pytest.raises(cap.CapabilityError, match="超时"):
        cap.shell_exec(policy, ["python", "-c", "import time; time.sleep(99)"],
                       cwd=str(workspace), timeout_s=2)
    assert captured["timeout"] == 2, "超时参数没有被传给 subprocess"
