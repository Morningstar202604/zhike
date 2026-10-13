"""回归：知识库批量导入/导出（标题模式 / 分块模式 / 往返闭环 / 边界）。"""
import json
from fastapi.testclient import TestClient

from core import retrieval


def _client(monkeypatch, tmp_path):
    # KB 文件与缓存全部隔离到临时目录，绝不碰仓库真实数据
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "kbi.db"))
    monkeypatch.setenv("ADMIN_TOKEN", "kb-admin")
    monkeypatch.setenv("CHANNEL_TOKEN", "kb-channel")
    kb_file = tmp_path / "kb.json"
    kb_file.write_text(json.dumps({"kb": []}), encoding="utf-8")
    monkeypatch.setattr(retrieval, "KB_PATH", kb_file)
    retrieval._KB_CACHE = None
    from core.main import create_app
    from core import store

    store.init_db()
    c = TestClient(create_app())
    c.headers.update({"X-Admin-Token": "kb-admin", "X-Channel-Token": "kb-channel"})
    return c


def test_import_block_mode(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    r = c.post("/api/kb/import", json={"text": "退货政策\n签收后 7 天可退。\n\n发货时效\n付款后 48 小时内发出。"})
    assert r.status_code == 200
    d = r.json()
    assert d["added"] == 2 and d["total"] == 2
    assert d["kb"][0]["id"] == "KB101" and d["kb"][0]["title"] == "退货政策"
    assert "7 天" in d["kb"][0]["answer"]
    assert d["kb"][1]["id"] == "KB102" and "48 小时" in d["kb"][1]["answer"]


def test_import_heading_mode_with_blank_lines_in_body(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    text = "## 支付方式\n支持微信、支付宝。\n\n也支持对公转账。\n\n## 发票\n可开电子发票。"
    d = c.post("/api/kb/import", json={"text": text}).json()
    assert d["added"] == 2
    assert d["kb"][0]["id"] == "KB101"
    assert "对公转账" in d["kb"][0]["answer"], "标题模式正文里的空行不应切断条目"
    assert d["kb"][1]["title"] == "发票"


def test_import_appends_and_ids_never_collide(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    c.post("/api/kb/import", json={"text": "第一条\n内容一。"})
    d2 = c.post("/api/kb/import", json={"text": "第二条\n内容二。"}).json()
    assert d2["added"] == 1 and d2["total"] == 2
    assert {e["id"] for e in d2["kb"]} == {"KB101", "KB102"}


def test_import_replace_rebuilds(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    c.post("/api/kb/import", json={"text": "旧的\n旧内容。"})
    d = c.post("/api/kb/import", json={"text": "新的\n新内容。", "replace": True}).json()
    assert d["added"] == 1 and d["total"] == 1
    assert d["kb"][0]["title"] == "新的"
    assert all(e["title"] != "旧的" for e in d["kb"])


def test_import_empty_and_garbage(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    assert c.post("/api/kb/import", json={"text": "   "}).status_code == 400
    d = c.post("/api/kb/import", json={"text": "只有一行标题"}).json()
    assert d["added"] == 1 and d["kb"][0]["answer"] == "只有一行标题"


def test_export_roundtrip(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    c.post("/api/kb/import", json={"text": "## 支付方式\n支持微信、支付宝。\n\n## 发票\n可开电子发票。"})
    exp = c.get("/api/kb/export")
    assert exp.status_code == 200
    assert "## 支付方式" in exp.text and "## 发票" in exp.text
    d = c.post("/api/kb/import", json={"text": exp.text, "replace": True}).json()
    assert d["added"] == 2, "导出的 Markdown 必须可直接再导入"
    assert {e["title"] for e in d["kb"]} == {"支付方式", "发票"}


def test_imported_entry_is_retrievable(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    c.post("/api/kb/import", json={"text": "物流时效\n国内主要城市 1-3 天送达，偏远地区 5 天。"})
    from core import retrieval

    hits = retrieval.retrieve("物流时效怎么样", top_k=3)
    assert any(h["title"] == "物流时效" for h in hits), "导入的新条目必须立即可检索"
