"""知识库检索：业界 2025-2026 小库标准方案 = FTS5(BM25 词法) + sqlite-vec(向量) + RRF 融合。

替代旧版手写 bigram 词重叠 + 内存 numpy 矩阵：
- FTS5 BM25：成熟词法打分，中文用 unicode61 + 自定义分词（简单按字 bigram 索引），比裸 bigram 更稳
- sqlite-vec：向量索引持久化到 SQLite，不再每次启动加载全量矩阵
- RRF（Reciprocal Rank Fusion）：两路排名融合，比分相加更稳，是向量库业界事实标准

降级链：sqlite-vec 不可用 → 纯 FTS5；FTS5 不可用（SQLite 没编）→ 退回 bigram 兜底。
KB 源文件仍是 kb.json（人可读、易编辑），SQLite 索引是它的派生缓存，mtime 变化自动重建。
"""
from __future__ import annotations

import json
import logging
import math
import pathlib
import re
import sqlite3
import threading

logger = logging.getLogger("cs.events")

KB_PATH = pathlib.Path(__file__).parent / "data" / "kb.json"
RETRIEVAL_DB = pathlib.Path(__file__).parent / "data" / "retrieval.db"

_KB_CACHE: tuple[str, int, list[dict]] | None = None
_INDEX_LOCK = threading.Lock()
_INDEX_STATE: dict = {"sig": "", "fts5": False, "vec": False}


# ---------- KB 源文件加载（保持人可读 JSON 为真相源） ----------

def load_kb() -> list[dict]:
    global _KB_CACHE
    mtime = KB_PATH.stat().st_mtime_ns
    key = str(KB_PATH)
    if _KB_CACHE is None or _KB_CACHE[0] != key or _KB_CACHE[1] != mtime:
        with open(KB_PATH, encoding="utf-8") as f:
            _KB_CACHE = (key, mtime, json.load(f)["kb"])
    return _KB_CACHE[2]


def bigrams(text: str) -> set[str]:
    """兜底用：FTS5/vec 都不可用时的纯词重叠。"""
    cjk = [ch for ch in text.lower() if "\u4e00" <= ch <= "\u9fff"]
    pairs = {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}
    return pairs | set(re.findall(r"[a-z0-9]{2,}", text.lower()))


# ---------- 向量后端（BGE-zh，成熟中文检索模型，CPU 可跑） ----------
BGE_MODEL = "BAAI/bge-small-zh-v1.5"
_COS_FLOOR, _COS_CEIL = 0.55, 0.90
_EMB_STATE: dict = {"model": None, "failed": False}
_BGE_QUERY_PREFIX = "为这个句子生成表示以检索相关文章："


def embedding_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("sentence_transformers") is not None


def _get_encoder():
    if _EMB_STATE["failed"]:
        return None
    if _EMB_STATE["model"] is None:
        try:
            from sentence_transformers import SentenceTransformer
            _EMB_STATE["model"] = SentenceTransformer(BGE_MODEL)
        except Exception as e:
            _EMB_STATE["failed"] = True
            logger.info("embedding backend unavailable: %s", str(e)[:80])
            return None
    return _EMB_STATE["model"]


# ---------- SQLite 索引层（FTS5 + vec） ----------

def _ensure_conn() -> sqlite3.Connection | None:
    """打开检索库；FTS5 / vec 扩展不可用时返回 None（降级到 bigram）。"""
    try:
        RETRIEVAL_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(RETRIEVAL_DB), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # 探测 FTS5
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(entry_id, title, text, tags, tokenize='unicode61')")
            _INDEX_STATE["fts5"] = True
        except sqlite3.Error:
            _INDEX_STATE["fts5"] = False
        # 探测 sqlite-vec
        try:
            import sqlite_vec  # type: ignore
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS kb_vec USING vec0(embedding float[512])")
            _INDEX_STATE["vec"] = True
        except Exception:
            _INDEX_STATE["vec"] = False
        return conn
    except Exception as e:
        logger.info("retrieval db init failed, fallback to bigram: %s", str(e)[:80])
        return None


def _doc_text(entry: dict) -> str:
    return f"{entry.get('title','')} {entry.get('text','')} {' '.join(entry.get('tags', []))}"


def rebuild_index_if_needed(conn: sqlite3.Connection, kb: list[dict]) -> None:
    """KB 文件 mtime 变化时重建 FTS5 + vec 索引（幂等）。"""
    sig = f"{KB_PATH}:{KB_PATH.stat().st_mtime_ns}:{len(kb)}"
    if _INDEX_STATE["sig"] == sig:
        return
    with _INDEX_LOCK:
        if _INDEX_STATE["sig"] == sig:
            return
        if _INDEX_STATE["fts5"]:
            conn.execute("DELETE FROM kb_fts")
            for e in kb:
                conn.execute(
                    "INSERT INTO kb_fts(entry_id, title, text, tags) VALUES (?,?,?,?)",
                    (e["id"], e.get("title", ""), e.get("text", ""), " ".join(e.get("tags", []))),
                )
        if _INDEX_STATE["vec"]:
            encoder = _get_encoder()
            if encoder is not None and kb:
                import numpy as np
                conn.execute("DELETE FROM kb_vec")
                docs = [_doc_text(e) for e in kb]
                embs = encoder.encode(docs, normalize_embeddings=True)
                for e, vec in zip(kb, embs):
                    conn.execute(
                        "INSERT INTO kb_vec(rowid, embedding) VALUES (?, ?)",
                        (hash(e["id"]) & 0x7FFFFFFFFFFFFFFF, np.asarray(vec, dtype="float32").tobytes()),
                    )
        conn.commit()
        _INDEX_STATE["sig"] = sig


# ---------- 检索：FTS5 + vec 两路 → RRF 融合 ----------

def _fts_search(conn: sqlite3.Connection, query: str, top_k: int) -> list[tuple[float, str]]:
    """FTS5 BM25 检索。返回 [(bm25_score, entry_id)]，score 越小越好（BM25 原生）。

    查询构造：CJK 按 bigram 切（避免单字 OR 太宽误召回），英文词原样，AND 组合。
    """
    if not _INDEX_STATE["fts5"] or not query.strip():
        return []
    cjk = [ch for ch in query if "\u4e00" <= ch <= "\u9fff"]
    terms = [f"{cjk[i]}{cjk[i+1]}" for i in range(len(cjk) - 1)]
    terms += re.findall(r"[a-zA-Z0-9]{2,}", query)
    if not terms:
        return []
    terms = list(dict.fromkeys(terms))[:10]
    fts_q = " AND ".join(f'"{t}"' for t in terms)
    try:
        rows = conn.execute(
            "SELECT entry_id, bm25(kb_fts) AS score FROM kb_fts WHERE kb_fts MATCH ? ORDER BY score LIMIT ?",
            (fts_q, top_k * 3),
        ).fetchall()
        return [(float(r["score"]), r["entry_id"]) for r in rows]
    except sqlite3.Error:
        return []


def _vec_search(conn: sqlite3.Connection, query: str, kb: list[dict], top_k: int) -> list[tuple[float, str]]:
    """向量检索。返回 [(distance, entry_id)]，distance 越小越好。"""
    if not _INDEX_STATE["vec"]:
        return []
    encoder = _get_encoder()
    if encoder is None:
        return []
    import numpy as np
    qv = encoder.encode([_BGE_QUERY_PREFIX + query], normalize_embeddings=True)[0]
    q_bytes = np.asarray(qv, dtype="float32").tobytes()
    try:
        rows = conn.execute(
            "SELECT rowid, distance FROM kb_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (q_bytes, top_k * 3),
        ).fetchall()
        rid_map = {hash(e["id"]) & 0x7FFFFFFFFFFFFFFF: e["id"] for e in kb}
        return [(float(r["distance"]), rid_map.get(r["rowid"], "")) for r in rows if r["rowid"] in rid_map]
    except Exception:
        return []


def _rrf_fuse(rankings: list[list[tuple[float, str]]], top_k: int, k: int = 60) -> list[tuple[float, str]]:
    """Reciprocal Rank Fusion：score = Σ 1/(k+rank)。业界事实标准，不用调权重。"""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, (_, eid) in enumerate(ranking):
            if not eid:
                continue
            scores[eid] = scores.get(eid, 0.0) + 1.0 / (k + rank + 1)
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [(s, eid) for eid, s in ranked[:top_k]]


def retrieve_scored(query: str, top_k: int, min_overlap: float, use_embedding: bool | None = None) -> list[tuple[float, dict]]:
    """混合检索：FTS5 BM25 + 向量，RRF 融合。返回 [(0~1 校准分, entry)]。"""
    kb = load_kb()
    if not kb:
        return []

    conn = _ensure_conn()
    if conn is not None:
        try:
            rebuild_index_if_needed(conn, kb)
            fts_hits = _fts_search(conn, query, top_k)
            vec_hits: list[tuple[float, str]] = []
            if use_embedding is not False:
                vec_hits = _vec_search(conn, query, kb, top_k)
            fused = _rrf_fuse([fts_hits, vec_hits], top_k)
            if fused:
                eid_map = {e["id"]: e for e in kb}
                # RRF 分数量纲 [0, 2/61≈0.033]，线性映射到 [0,1]
                max_s = fused[0][0] if fused else 1.0
                out = []
                for s, eid in fused:
                    if eid not in eid_map:
                        continue
                    norm = s / max(max_s, 1e-9)
                    if norm < min_overlap:
                        continue
                    out.append((norm, eid_map[eid]))
                return out
        finally:
            if conn is not None and hasattr(conn, "close"):
                conn.close()

    # ---- 兜底：纯 bigram（FTS5/vec 都不可用时） ----
    q_big = bigrams(query)
    scored = []
    for entry in kb:
        doc_text = _doc_text(entry)
        overlap = len(q_big & bigrams(doc_text))
        score = overlap / max(len(q_big), 1) if overlap else 0.0
        if score <= 0 or score < min_overlap:
            continue
        scored.append((score, entry))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_k]


def retrieve(query: str, top_k: int = 4, min_overlap: float = 0.0, use_embedding: bool | None = None) -> list[dict]:
    return [e for _, e in retrieve_scored(query, top_k, min_overlap, use_embedding)]
