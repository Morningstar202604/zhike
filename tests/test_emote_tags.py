"""表情包挑选：视觉标签匹配 + 去重 + 发给会话的回归。"""
import json

from gateway.humanize import EmoteLib
from core.tool_context import ToolContext


def _lib(tmp_path, items, tags=None):
    idx = tmp_path / "index.json"
    idx.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    if tags is not None:
        (tmp_path / "tags.json").write_text(json.dumps(tags, ensure_ascii=False), encoding="utf-8")
    return EmoteLib(str(idx), tags_path=str(tmp_path / "tags.json"))


def test_pick_matches_vision_tags_when_summary_is_blank(tmp_path):
    """群里收的梗图 summary 全是「[动画表情]」，只能靠视觉标签命中关键词。"""
    lib = _lib(tmp_path, [
        {"kind": "image", "summary": "[动画表情]", "data": "a.gif"},
        {"kind": "image", "summary": "[动画表情]", "data": "b.gif"},
    ], tags={"a.gif": "猫，得意，叉腰", "b.gif": "熊猫头，无语"})
    assert lib.pick("猫")["data"] == "a.gif"
    assert lib.pick("无语")["data"] == "b.gif"


def test_pick_falls_back_to_random_when_no_match(tmp_path):
    lib = _lib(tmp_path, [{"kind": "image", "summary": "[动画表情]", "data": "a.gif"}],
               tags={"a.gif": "猫，得意"})
    assert lib.pick("完全不相干的词")["data"] == "a.gif"


def test_pick_avoids_immediate_repeat(tmp_path):
    items = [{"kind": "image", "summary": "", "data": f"{i}.gif"} for i in range(4)]
    tags = {f"{i}.gif": "猫，得意" for i in range(4)}
    lib = _lib(tmp_path, items, tags)
    picks = [lib.pick("猫")["data"] for _ in range(4)]
    assert len(set(picks)) == 4  # 连续四次不重样


def test_render_uses_local_file_cq(tmp_path):
    lib = _lib(tmp_path, [{"kind": "image", "summary": "", "data": "C:/x/cat.gif"}],
               tags={"C:/x/cat.gif": "猫，笑"})
    out = lib.render("给你 [表情包:猫]")
    assert "CQ:image" in out and "cat.gif" in out


def test_render_drops_marker_when_library_empty(tmp_path):
    lib = _lib(tmp_path, [])
    assert lib.render("给你 [表情包:猫]").strip() == "给你"


def test_search_emote_reads_tags(tmp_path, monkeypatch):
    from core import tools

    (tmp_path / "index.json").write_text(json.dumps(
        [{"kind": "image", "summary": "[动画表情]", "data": "a.gif"}], ensure_ascii=False),
        encoding="utf-8")
    (tmp_path / "tags.json").write_text(json.dumps({"a.gif": "狗，委屈，冒个泡"}, ensure_ascii=False),
                                        encoding="utf-8")
    monkeypatch.setenv("GATEWAY_EMOTE_DIR", str(tmp_path))
    ctx = ToolContext()
    r = tools.tool_search_emote(ctx, keyword="狗")
    assert r["found"] and "狗" in r["note"]
    assert r["sample_path"] == "a.gif"


def test_pick_expands_emotion_synonyms(tmp_path):
    """模型写「开心」，库里标的是「得意」——同义词必须能命中，否则等于随机甩图。"""
    lib = _lib(tmp_path, [
        {"kind": "image", "summary": "", "data": "happy.gif"},
        {"kind": "image", "summary": "", "data": "sad.gif"},
    ], tags={"happy.gif": "狗，得意，叉腰", "sad.gif": "人，委屈，冒个泡"})
    assert lib.pick("开心")["data"] == "happy.gif"
    assert lib.pick("委屈")["data"] == "sad.gif"


def test_expand_keywords_keeps_original_first():
    from gateway.humanize import _expand_keywords

    out = _expand_keywords("猫")
    assert out[0] == "猫" and "猫娘" in out
    assert _expand_keywords("") == []


def test_admin_emotes_api_exposes_vision_tags(tmp_path, monkeypatch):
    """管理接口 /api/emotes 必须返回视觉标签。

    回归背景：标签存在独立的 tags.json（按 data 路径索引），不在 index.json 条目里。
    接口早期直接读 it["tags"]，永远拿到 None，控制台只能回退显示 "image"，
    按标签搜索也搜不到——打标脚本跑了 285 张，界面上看不出任何效果。
    """
    import base64
    import secrets

    from fastapi.testclient import TestClient

    (tmp_path / "index.json").write_text(json.dumps(
        [{"kind": "image", "summary": "[动画表情]", "data": "a.gif"},
         {"kind": "image", "summary": "[动画表情]", "data": "b.gif"}], ensure_ascii=False),
        encoding="utf-8")
    (tmp_path / "tags.json").write_text(json.dumps(
        {"a.gif": "熊猫头，无语", "b.gif": "猫，得意"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("GATEWAY_EMOTE_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_TOKEN", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))

    from core import security
    from core.main import create_app

    client = TestClient(create_app())
    r = client.get("/api/emotes", headers={"X-Admin-Token": security.admin_token()})
    assert r.status_code == 200, r.text
    tags = {it["data"]: it["tags"] for it in r.json()["items"]}
    assert tags["a.gif"] == "熊猫头，无语"
    assert tags["b.gif"] == "猫，得意"

    # 按标签过滤也必须命中，而不是只在 summary 里找。
    # 用「得意」而不是「猫」：熊猫头本身含「猫」字，会被子串匹配到，指标不干净。
    r2 = client.get("/api/emotes", params={"q": "得意"}, headers={"X-Admin-Token": security.admin_token()})
    assert r2.status_code == 200, r2.text
    assert [it["data"] for it in r2.json()["items"]] == ["b.gif"]
