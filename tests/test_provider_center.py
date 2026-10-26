"""providers / skills / hooks / permissions 四组能力的回归用例。

每组都对应一个真实缺口，不是「为覆盖率写测试」：
- providers：地址+Key 要能发现模型、匹配能力、真发一次请求验证连通
- skills：注入系统提示词；内置技能删不掉、只能停用
- hooks：pre_input 拦截/改写、post_reply 追加/替换，且坏正则不能污染配置
- permissions：open_mode 真的影响 Agent 档位；锁定项不能从接口改
"""
import asyncio

import pytest

from core import hooks, permissions, providers, skills


# ---------------- providers ----------------

def test_parse_models_filters_non_chat_and_reads_capabilities():
    payload = {"data": [
        {"id": "u2-flash", "context_window": 1048576, "max_output": 131072},
        {"id": "text-embedding-3", "context_window": 8192},
        {"id": "whisper-1"},
        {"id": "gpt-4o", "context_length": 128000, "max_completion_tokens": 16384},
    ]}
    got = {m.id: m for m in providers.parse_models(payload, "openai-compatible")}
    assert set(got) == {"u2-flash", "gpt-4o"}
    assert got["u2-flash"].context_window == 1_048_576
    assert got["u2-flash"].max_output == 131_072
    assert got["gpt-4o"].max_output == 16_384
    assert got["u2-flash"].source == "reported"


def test_parse_models_infers_capabilities_when_absent():
    got = providers.parse_models({"data": [{"id": "claude-sonnet-4"}]}, "openai-compatible")
    assert got[0].context_window == 200_000
    assert got[0].source == "auto"
    assert got[0].tool_calling is True


def test_parse_models_never_reports_output_above_context():
    got = providers.parse_models(
        {"data": [{"id": "weird", "context_window": 4096, "max_output": 65536}]},
        "openai-compatible")
    assert got[0].max_output == 4096


def test_parse_gemini_payload():
    payload = {"models": [{
        "name": "models/gemini-2.5-pro", "displayName": "Gemini 2.5 Pro",
        "inputTokenLimit": 1_048_576, "outputTokenLimit": 65_536,
        "supportedGenerationMethods": ["generateContent"],
    }]}
    got = providers.parse_models(payload, "google-gemini")
    assert got[0].id == "gemini-2.5-pro"
    assert got[0].context_window == 1_048_576
    assert got[0].display_name == "Gemini 2.5 Pro"


def test_metadata_host_still_rejected_for_private_endpoint():
    req = providers.ProviderRequest(base_url="http://169.254.169.254/latest",
                                    private_ok=True, api_key="k")
    out = asyncio.run(providers.discover(req))
    assert out["ok"] is False
    assert "元数据" in out["error"]


def test_discover_requires_key_for_openai(monkeypatch):
    req = providers.ProviderRequest(base_url="https://api.example.com/v1")
    out = asyncio.run(providers.discover(req))
    assert out["ok"] is False
    assert "API Key" in out["error"]


# ---------------- skills ----------------

def test_builtin_skills_cannot_be_deleted():
    kept = skills.normalize_items([{"id": "custom-x", "name": "自定义", "guide": "g"}])
    ids = {s["id"] for s in kept}
    for b in skills.BUILTIN_SKILLS:
        assert b["id"] in ids, "内置技能被删掉了"


def test_skill_render_only_includes_enabled():
    items = skills.normalize_items([
        {"id": "a", "name": "开启的", "guide": "做 A", "enabled": True},
        {"id": "b", "name": "关闭的", "guide": "做 B", "enabled": False},
    ])
    text = skills.render({"skills": {"items": items}})
    assert "开启的" in text and "做 A" in text
    assert "关闭的" not in text


def test_skill_render_empty_when_all_disabled():
    items = [dict(b, enabled=False) for b in skills.BUILTIN_SKILLS]
    assert skills.render({"skills": {"items": items}}) == ""


# ---------------- hooks ----------------

def test_pre_input_block():
    cfg = {"hooks": {"items": [{
        "id": "h1", "name": "拦机密", "event": "pre_input",
        "match": "机密", "action": "block", "value": "不能说", "enabled": True,
    }]}}
    out = hooks.run_pre_input("这是机密内容", cfg)
    assert out["action"] == "block" and out["text"] == "不能说"


def test_pre_input_rewrite():
    cfg = {"hooks": {"items": [{
        "id": "h2", "name": "改称呼", "event": "pre_input",
        "match": "老板", "action": "rewrite", "value": "同事", "enabled": True,
    }]}}
    out = hooks.run_pre_input("老板你好", cfg)
    assert out["action"] == "rewrite" and out["text"] == "同事你好"


def test_post_reply_append_and_replace():
    append_cfg = {"hooks": {"items": [{
        "id": "h3", "name": "签名", "event": "post_reply", "match": "",
        "action": "append", "value": "— 秘书", "enabled": True,
    }]}}
    out = hooks.run_post_reply("好的", append_cfg)
    assert out["action"] == "append" and out["text"].endswith("— 秘书")

    replace_cfg = {"hooks": {"items": [{
        "id": "h4", "name": "替换", "event": "post_reply", "match": "内部",
        "action": "replace", "value": "已隐去", "enabled": True,
    }]}}
    out2 = hooks.run_post_reply("这是内部信息", replace_cfg)
    assert out2["action"] == "replace" and out2["text"] == "已隐去"


def test_invalid_regex_hook_is_dropped_not_crashing():
    items = hooks.normalize_items([
        {"id": "bad", "event": "pre_input", "action": "block", "match": "([", "value": "x"},
        {"id": "good", "event": "pre_input", "action": "block", "match": "ok", "value": "y"},
    ])
    assert [h["id"] for h in items] == ["good"]
    out = hooks.run_pre_input("随便说点什么", {"hooks": {"items": items}})
    assert out["action"] == "pass"


def test_hook_disabled_is_skipped():
    cfg = {"hooks": {"items": [{
        "id": "h5", "event": "pre_input", "match": "", "action": "block",
        "value": "x", "enabled": False,
    }]}}
    assert hooks.run_pre_input("hi", cfg)["action"] == "pass"


# ---------------- permissions ----------------

def test_locked_item_cannot_be_changed_through_api():
    cfg = {"permissions": {"locked": ["auth", "secrets"]}}
    with pytest.raises(ValueError):
        permissions.validate_patch(cfg, {"locked": ["auth"]})


def test_open_mode_can_be_toggled_and_caps_tier():
    cfg = {"permissions": {"open_mode": False}}
    assert permissions.effective_agent_tier(cfg, 3) == 1
    assert permissions.effective_agent_tier(cfg, 0) == 0
    cfg2 = {"permissions": {"open_mode": True}}
    assert permissions.effective_agent_tier(cfg2, 3) == 3


def test_describe_lists_locked_and_open():
    out = permissions.describe({"permissions": {"open_mode": True}})
    assert out["open_mode"] is True
    assert {x["id"] for x in out["locked"]} == set(permissions.LOCKED_ITEMS)
    assert out["open"], "可改项列表不能为空"


# ---------------- API 契约 ----------------

def test_skills_api_roundtrip(client):
    r = client.put("/api/skills", json={"items": [
        {"id": "custom-1", "name": "竞品调研", "description": "对比竞品", "guide": "先搜再列对比表"},
    ]})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert any(s["name"] == "竞品调研" for s in items)
    # 内置技能仍在
    assert any(s["builtin"] for s in items)
    listed = client.get("/api/skills").json()["items"]
    assert any(s["name"] == "竞品调研" for s in listed)


def test_hooks_api_roundtrip(client):
    r = client.put("/api/hooks", json={"items": [{
        "id": "h-api", "name": "签名", "event": "post_reply",
        "match": "", "action": "append", "value": "— 由知客回复",
    }]})
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["name"] == "签名"
    assert client.get("/api/hooks").json()["items"][0]["id"] == "h-api"


def test_permissions_api_rejects_locked_change(client):
    ok = client.put("/api/permissions", json={"open_mode": True})
    assert ok.status_code == 200
    bad = client.put("/api/permissions", json={"locked": ["auth"]})
    assert bad.status_code == 400


def test_providers_presets_api(client):
    r = client.get("/api/providers/presets")
    assert r.status_code == 200
    body = r.json()
    names = {p["name"] for p in body["presets"]}
    assert {"OpenAI", "Anthropic Claude", "Google Gemini"} <= names
    assert {p["value"] for p in body["protocols"]} == set(providers.PROTOCOLS)


def test_providers_discover_requires_admin(client, monkeypatch):
    # 去掉管理员令牌后必须 401/503，不能匿名触发服务器出站请求
    r = client.post("/api/providers/discover", json={"base_url": "https://example.com/v1"},
                    headers={"X-Admin-Token": "wrong"})
    assert r.status_code in (401, 503)


def test_post_reply_append_is_idempotent():
    """模型自己写了落款时不能再追加一遍（实测叠成两行）。"""
    from core import hooks

    cfg = {"hooks": {"items": [{
        "id": "sig", "name": "落款", "event": "post_reply", "match": "",
        "action": "append", "value": "—— 知客 · 你的私人秘书", "enabled": True,
    }]}}
    once = hooks.run_post_reply("好的", cfg)
    assert once["text"].count("—— 知客") == 1
    twice = hooks.run_post_reply(once["text"], cfg)
    assert twice["text"].count("—— 知客") == 1, "已经有落款就不能再追加"
    assert twice["action"] == "pass"
