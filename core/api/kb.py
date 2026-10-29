"""知识库路由：CRUD / 批量导入 / 文档导入 / 试召回 / 导出 / 坐席回流。

从 core/main.py 抽出（原文件把 110 个路由塞在一个 create_app 闭包里，
无法单独测试、无法按职责审阅，也无法做路由级限流）。

本模块只做「参数校验 + 调用 store」，不碰文件锁与原子写——
那是 core/kb_store.py 的职责。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from core import config, docparse, kb_store, retrieval
from core.api.deps import require_admin

router = APIRouter(prefix="/api/kb", tags=["knowledge-base"])


class KbIn(BaseModel):
    id: str
    text: str = ""
    title: str = ""
    answer: str = ""
    tags: list[str] = []


class KbImportIn(BaseModel):
    text: str
    replace: bool = False


class KbTestIn(BaseModel):
    query: str


class KbSaveNoteIn(BaseModel):
    title: str = ""
    text: str
    tags: list[str] = []


@router.get("", dependencies=[Depends(require_admin)])
def kb_list():
    emb_flag = config.get_config().get("retrieval", {}).get("embedding", "auto")
    available = retrieval.embedding_available()
    return {
        "kb": retrieval.load_kb(),
        "embedding": {
            "available": available,
            "enabled": emb_flag,
            "model": retrieval.BGE_MODEL if available else "",
        },
    }


@router.post("", dependencies=[Depends(require_admin)])
def add_kb(body: KbIn):
    def op(items):
        if any(e["id"] == body.id for e in items):
            raise HTTPException(400, "知识库条目 ID 已存在")
        items.append({"id": body.id, "text": body.text, "title": body.title,
                      "answer": body.answer, "tags": body.tags})
        return None, items

    _, items = kb_store.store.mutate(op)
    return {"kb": items}


@router.put("/{kb_id}", dependencies=[Depends(require_admin)])
def update_kb(kb_id: str, body: KbIn):
    def op(items):
        for i, e in enumerate(items):
            if e["id"] == kb_id:
                items[i] = {"id": kb_id, "text": body.text, "title": body.title,
                            "answer": body.answer, "tags": body.tags}
                return None, items
        raise HTTPException(404, "知识库条目不存在")

    _, items = kb_store.store.mutate(op)
    return {"kb": items}


@router.delete("/{kb_id}", dependencies=[Depends(require_admin)])
def delete_kb(kb_id: str):
    def op(items):
        new = [e for e in items if e["id"] != kb_id]
        if len(new) == len(items):
            raise HTTPException(404, "知识库条目不存在")
        return None, new

    _, new = kb_store.store.mutate(op)
    return {"kb": new}


@router.post("/import", dependencies=[Depends(require_admin)])
def import_kb(body: KbImportIn):
    """批量导入：自动识别「## 标题」或空行分块两种格式，条目 id 自动编号（KB101 起）。

    replace=True 时清空现有条目后导入（整库重建）。
    答案里行首出现「## 」会破坏再导入，会在解析阶段被当成新条目标题。
    """
    from core.api import deps as deps_mod

    # 每次请求读模块属性而不是 import 时快照：测试与运维都靠 monkeypatch 调这个上限
    limit = deps_mod.MAX_KB_IMPORT_ITEMS
    blocks = kb_store.parse_blocks(body.text)
    if not blocks:
        raise HTTPException(400, "未能解析出任何条目：用「## 标题」开头，或用空行分隔「标题/正文」块")
    if len(blocks) > limit:
        raise HTTPException(413, f"单次导入条目过多（{len(blocks)} > {limit}），请分批导入")

    def op(items):
        if body.replace:
            items = []
        base = kb_store.next_base(items)
        for title, answer in blocks:
            base += 1
            items.append({
                "id": f"KB{base}",
                "title": title[:80],
                "text": f"{title} {answer}"[:300],
                "answer": answer or title,
                "tags": [],
            })
        return None, items

    _, items = kb_store.store.mutate(op)
    return {"added": len(blocks), "total": len(items), "kb": items}


@router.post("/import/document", dependencies=[Depends(require_admin)])
async def import_kb_document(file: UploadFile):
    """文档导入：PDF/Word/Excel/Markdown → 解析 → 标题/段落分块 → KB 条目（追加语义）。"""
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "文档超过 10MB 上限")
    fname = file.filename or "doc"
    try:
        text = docparse.extract_text(fname, data)
        blocks = docparse.chunk_document(text, fname.rsplit(".", 1)[0])
    except RuntimeError as e:  # 可选依赖缺失/文件损坏：转可读 400
        raise HTTPException(400, str(e))
    if not blocks:
        raise HTTPException(400, "未能从文档中解析出内容")

    def op(items):
        base = kb_store.next_base(items)
        for title, chunk in blocks:
            base += 1
            items.append({
                "id": f"KB{base}",
                "title": title[:80],
                "text": f"{title} {chunk}"[:300],
                "answer": chunk,
                "tags": ["文档"],
            })
        return None, items

    _, items = kb_store.store.mutate(op)
    return {"added": len(blocks), "total": len(items), "file": fname}


@router.post("/test", dependencies=[Depends(require_admin)])
def kb_test(body: KbTestIn):
    """试召回：按当前生效配置（top-K/门槛/向量开关）跑一次检索，返回命中与分数。"""
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(400, "检索词不能为空")
    cfg = config.get_config()
    top_k = cfg.get("retrieval", {}).get("kb_top_k", 4)
    min_overlap = cfg.get("retrieval", {}).get("min_overlap", 0.0)
    emb_flag = cfg.get("retrieval", {}).get("embedding", "auto")
    use_emb = None if emb_flag == "auto" else (emb_flag == "on")
    hits = retrieval.retrieve_scored(q, top_k, min_overlap, use_embedding=use_emb)
    return {
        "query": q,
        "top_k": top_k,
        "min_overlap": min_overlap,
        "embedding": emb_flag,
        "hits": [
            {"id": e["id"], "title": e["title"], "score": round(s, 3),
             "answer": (e["answer"] or "")[:120]}
            for s, e in hits
        ],
    }


@router.get("/export", dependencies=[Depends(require_admin)])
def export_kb():
    """导出为 Markdown（可再次导入，形成备份闭环）。"""
    return PlainTextResponse(
        kb_store.export_markdown(retrieval.load_kb()),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=zhike-kb.md"},
    )


@router.post("/save-note", dependencies=[Depends(require_admin)])
def kb_save_note(body: KbSaveNoteIn):
    """坐席答案回流：把人工回复存为 KB 条目，沉淀为下次的自动答案。"""
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "内容不能为空")
    title = (body.title or text.strip().splitlines()[0] or text[:30]).strip()[:80]

    def op(items):
        entry = {
            "id": f"KB{kb_store.next_base(items) + 1}",
            "title": title,
            "text": f"{title} {text}"[:300],
            "answer": text,
            "tags": body.tags or ["坐席回流"],
        }
        items.append(entry)
        return entry, items

    entry, items = kb_store.store.mutate(op)
    return {"entry": entry, "total": len(items)}
