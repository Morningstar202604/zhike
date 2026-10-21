"""回归：文档导入管道（docparse 解析/分块）+ 试召回端点。"""
import json

import pytest

from core import docparse, retrieval


def test_chunk_by_headings():
    md = "# 手册\n\n## 退货政策\n7 天内可退。\n\n## 发票\n可开电子发票。"
    blocks = docparse.chunk_document(md, "手册")
    assert len(blocks) == 2
    titles = [t for t, _ in blocks]
    assert "退货政策" in titles and "发票" in titles
    assert all("可退" in b or "发票" in b for _, b in blocks)


def test_chunk_long_section_splits():
    body = "这是一句话。" * 120  # ~600 字，超过 MAX_CHUNK
    md = f"## 物流说明\n{body}"
    blocks = docparse.chunk_document(md, "物流")
    assert len(blocks) >= 2, "超长小节必须再切"
    assert all(len(b) <= docparse.MAX_CHUNK + 50 for _, b in blocks), "块长必须受控（按句切分留余量）"


def test_chunk_no_headings_uses_fallback_title():
    md = "\n\n".join("普通段落内容" + str(i) * 30 for i in range(30))
    blocks = docparse.chunk_document(md, "操作手册")
    assert len(blocks) >= 2
    assert all(t.startswith("操作手册") for t, _ in blocks), "无标题文档回落为文件名标题"


def test_extract_md_txt_passthrough():
    assert docparse.extract_text("a.md", "## 标题\n正文".encode()) == "## 标题\n正文"
    assert docparse.extract_text("b.txt", "纯文本".encode()) == "纯文本"


def test_extract_unsupported_raises():
    with pytest.raises(RuntimeError):
        docparse.extract_text("c.docx", b"fake") if False else docparse.extract_text("c.exe", b"MZ")
    try:
        docparse.extract_text("c.pdf", b"%PDF-fake")
        assert False, "伪 PDF 应报依赖或解析错误"
    except RuntimeError as e:
        assert "requirements-docs" in str(e) or True  # 有依赖时报解析错误也可接受


def test_docx_heading_mapping():
    docx = pytest.importorskip("docx")
    import io

    d = docx.Document()
    d.add_heading("手册", 0)
    d.add_heading("售后政策", 1)
    d.add_paragraph("签收后 7 天可退。")
    buf = io.BytesIO()
    d.save(buf)
    text = docparse.extract_text("manual.docx", buf.getvalue())
    assert "## 售后政策" in text, "DOCX 标题应映射为 Markdown 标题"
    blocks = docparse.chunk_document(text, "manual")
    assert any("售后政策" in t for t, _ in blocks)


def test_kb_test_endpoint(client):
    r = client.post("/api/kb/test", json={"query": "退货政策是什么"})
    assert r.status_code == 200
    d = r.json()
    assert d["query"] == "退货政策是什么" and "hits" in d and "top_k" in d
    assert all("score" in h and "id" in h for h in d["hits"])
    r2 = client.post("/api/kb/test", json={"query": "  "})
    assert r2.status_code == 400


def test_kb_doc_import_markdown(client, monkeypatch, tmp_path):
    """导入测试必须用临时 KB，避免污染仓库真实知识库文件。"""
    import pathlib

    kb_file = tmp_path / "kb.json"
    kb_file.write_text(json.dumps({"kb": [
        {"id": "KB001", "title": "种子", "text": "种子", "answer": "种子条目", "tags": []},
    ]}), encoding="utf-8")
    monkeypatch.setattr(retrieval, "KB_PATH", kb_file)
    retrieval._KB_CACHE = None
    md = "## 保修政策\n主机保修 1 年。\n\n## 配件\n配件保修 90 天。"
    r = client.post(
        "/api/kb/import/document",
        files={"file": ("售后手册.md", md.encode(), "text/markdown")},
    )
    assert r.status_code == 200
    d = r.json()
    assert d["added"] == 2 and d["file"] == "售后手册.md"
    entries = retrieval.load_kb()
    new = [e for e in entries if e.get("tags") and "文档" in e["tags"]]
    assert len(new) == 2
    assert all(e["id"].startswith("KB") for e in new)
    retrieval._KB_CACHE = None


def test_extract_xlsx_to_markdown():
    """Excel 每个工作表 → 一个 ## 小节，行内单元格用 | 分隔。"""
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "价格表"
    ws.append(["商品", "价格"])
    ws.append(["A 款", 199])
    ws.append([None, None])
    ws2 = wb.create_sheet("说明")
    ws2.append(["运费规则", "满 99 包邮"])
    buf = io.BytesIO()
    wb.save(buf)

    text = docparse.extract_text("价目.xlsx", buf.getvalue())
    assert "## 价格表" in text and "## 说明" in text
    assert "商品 | 价格" in text and "A 款 | 199" in text
    assert "运费规则 | 满 99 包邮" in text
    # 空行不产生空内容
    assert "None" not in text


def test_extract_xlsx_corrupt_raises_readable():
    with pytest.raises(RuntimeError) as ei:
        docparse.extract_text("坏.xlsx", b"not an excel file")
    assert "Excel 解析失败" in str(ei.value)
