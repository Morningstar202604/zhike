"""知识库检索：业界 2025-2026 小库标准方案 = FTS5(BM25 词法) + sqlite-vec(向量) + RRF 融合。

替代旧版手写 bigram 词重叠 + 内存 numpy 矩阵：
- FTS5 BM25：成熟词法打分，中文用 unicode61 + 自定义分词（简单按字 bigram 索引），比裸 bigram 更稳
- sqlite-vec：向量索引持久化到 SQLite，不再每次启动加载全量矩阵
- RRF（Reciprocal Rank Fusion）：两路排名融合，比分相加更稳，是向量库业界事实标准

降级链：sqlite-vec 不可用 → 纯 FTS5；FTS5 不可用（SQLite 没编）→ 退回 bigram 兜底。
KB 源文件仍是 kb.json（人可读、易编辑），SQLite 索引是它的派生缓存，mtime 变化自动重建。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import pathlib
import re
import sqlite3
import threading

logger = logging.getLogger("cs.events")

# 路径可由环境变量覆盖（容器里指向持久卷，避免重建镜像丢知识库）
KB_PATH = pathlib.Path(os.environ.get("CS_AGENT_KB") or (pathlib.Path(__file__).parent / "data" / "kb.json"))
RETRIEVAL_DB = pathlib.Path(os.environ.get("CS_AGENT_RETRIEVAL_DB") or (pathlib.Path(__file__).parent / "data" / "retrieval.db"))

# 分词方案版本：索引结构或切词算法变化时递增，触发全量重建
TOKENIZER_VERSION = 2

_KB_CACHE: tuple[str, int, list[dict]] | None = None
_INDEX_LOCK = threading.Lock()
_INDEX_STATE: dict = {"sig": "", "fts5": False, "vec": False}


# ---------- KB 源文件加载（保持人可读 JSON 为真相源） ----------

def load_kb() -> list[dict]:
    global _KB_CACHE
    key = str(KB_PATH)
    try:
        mtime = KB_PATH.stat().st_mtime_ns
    except FileNotFoundError:
        # 部署到持久卷时首次启动可能还没有文件：返回空库，不抛异常打断服务启动
        _KB_CACHE = (key, -1, [])
        return _KB_CACHE[2]
    if _KB_CACHE is None or _KB_CACHE[0] != key or _KB_CACHE[1] != mtime:
        with open(KB_PATH, encoding="utf-8") as f:
            _KB_CACHE = (key, mtime, json.load(f)["kb"])
    return _KB_CACHE[2]


def seed_kb_if_missing(seed_path: pathlib.Path | None = None) -> bool:
    """持久化路径下首次启动无 kb.json 时，从镜像内置种子复制一份。返回是否发生复制。"""
    if KB_PATH.exists():
        return False
    seed = seed_path or (pathlib.Path(__file__).parent / "data" / "kb.json")
    if not seed.exists() or seed.resolve() == KB_PATH.resolve():
        return False
    KB_PATH.parent.mkdir(parents=True, exist_ok=True)
    KB_PATH.write_bytes(seed.read_bytes())
    logger.info("seeded knowledge base: %s -> %s", seed, KB_PATH)
    return True


def bigrams(text: str) -> set[str]:
    """兜底用：FTS5/vec 都不可用时的纯词重叠。"""
    cjk = [ch for ch in text.lower() if "\u4e00" <= ch <= "\u9fff"]
    pairs = {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}
    return pairs | set(re.findall(r"[a-z0-9]{2,}", text.lower()))


def bigram_text(text: str) -> str:
    """把文本切成「FTS5 可索引的空格分隔 token」：中文按 bigram 展开，英文/数字按词保留。

    索引侧与查询侧必须调用同一个函数，否则会出现「索引用整段中文、查询用 bigram」的错配：
    unicode61 不会切分无空格中文，整段存成一个 token，任何中文子串都查不到。
    """
    out: list[str] = []
    for run in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", text or ""):
        if run[0].isascii():
            out.append(run.lower())
        else:
            out += [run[i] + run[i + 1] for i in range(len(run) - 1)]
    return " ".join(out)


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
        # 索引签名表：跨进程共享"当前索引对应哪版 KB/分词"，避免多 worker 互相重建
        conn.execute("CREATE TABLE IF NOT EXISTS index_meta (k TEXT PRIMARY KEY, v TEXT)")
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


def _kb_rowid(entry_id: str) -> int:
    """KB 条目 → vec0 稳定 rowid。

    不能用内置 hash()：Python 对 str 的哈希每进程随机化（PYTHONHASHSEED），
    建索引与查询分处不同进程/重启后 rowid 就对不上，向量检索会全部 miss。
    """
    digest = hashlib.sha256(entry_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


def _read_sig(conn: sqlite3.Connection) -> str:
    """索引签名持久化在检索库里。

    只放进程内存时，第二个 worker 进程永远认为索引过期 → 各自 DELETE 重建，
    多 worker 下会互相把对方的索引删掉。落库后所有进程看到同一个签名。
    """
    try:
        row = conn.execute("SELECT v FROM index_meta WHERE k = 'sig'").fetchone()
        return row["v"] if row else ""
    except sqlite3.Error:
        return ""


def _write_sig(conn: sqlite3.Connection, sig: str) -> None:
    conn.execute(
        "INSERT INTO index_meta(k, v) VALUES ('sig', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (sig,),
    )


def rebuild_index_if_needed(conn: sqlite3.Connection, kb: list[dict]) -> None:
    """KB 文件 mtime 或分词版本变化时重建 FTS5 + vec 索引（幂等）。"""
    sig = f"{KB_PATH}:{KB_PATH.stat().st_mtime_ns}:{len(kb)}:{TOKENIZER_VERSION}"
    # 先看进程内缓存，再核对库里的签名（跨进程一致）
    if _INDEX_STATE["sig"] == sig and _read_sig(conn) == sig:
        return
    with _INDEX_LOCK:
        if _INDEX_STATE["sig"] == sig and _read_sig(conn) == sig:
            return
        if _INDEX_STATE["fts5"]:
            conn.execute("DELETE FROM kb_fts")
            for e in kb:
                # 存切分后的 token 串：unicode61 不切中文，必须索引侧预先 bigram 展开
                conn.execute(
                    "INSERT INTO kb_fts(entry_id, title, text, tags) VALUES (?,?,?,?)",
                    (
                        e["id"],
                        bigram_text(e.get("title", "")),
                        bigram_text(e.get("text", "")),
                        bigram_text(" ".join(e.get("tags", []))),
                    ),
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
                        (_kb_rowid(e["id"]), np.asarray(vec, dtype="float32").tobytes()),
                    )
        conn.commit()
        _write_sig(conn, sig)
        conn.commit()
        _INDEX_STATE["sig"] = sig


# ---------- 检索：FTS5 + vec 两路 → RRF 融合 ----------

def _fts_search(conn: sqlite3.Connection, query: str, top_k: int) -> list[tuple[float, str]]:
    """FTS5 BM25 检索。返回 [(bm25_score, entry_id)]，score 越小越好（BM25 原生）。

    查询构造与索引侧共用 bigram_text：中文 bigram、英文整词，AND 组合（避免单字 OR 过宽）。
    索引 token 不全命中时退回 OR，保证长问句至少能捞到部分匹配。
    """
    if not _INDEX_STATE["fts5"] or not query.strip():
        return []
    terms = list(dict.fromkeys(bigram_text(query).split()))[:10]
    if not terms:
        return []

    def _query(joiner: str) -> list[tuple[float, str]]:
        fts_q = joiner.join(f'"{t}"' for t in terms)
        rows = conn.execute(
            "SELECT entry_id, bm25(kb_fts) AS score FROM kb_fts WHERE kb_fts MATCH ? ORDER BY score LIMIT ?",
            (fts_q, top_k * 3),
        ).fetchall()
        return [(float(r["score"]), r["entry_id"]) for r in rows]

    try:
        hits = _query(" AND ")
        if hits:
            return hits
        # AND 全落空（长问句里总有一个 bigram 不在词表）→ OR 兜底，保留可召回性
        return _query(" OR ")
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
        rid_map = {_kb_rowid(e["id"]): e["id"] for e in kb}
        return [(float(r["distance"]), rid_map.get(r["rowid"], "")) for r in rows if r["rowid"] in rid_map]
    except Exception:
        return []


_RRF_K = 60


def _rrf_fuse(rankings: list[list[tuple[float, str]]], top_k: int, k: int = _RRF_K) -> list[tuple[float, str]]:
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
                # 分数标定：不能简单地 s/max_s。
                # 那样第一名恒为 1.0，min_overlap 设成 0.99 也拦不住它——阈值语义失效。
                # 正确做法：用 RRF 的理论上界换算成绝对强度。
                #   单路排名第 1 得 1/(k+1)，两路都第 1 得 2/(k+1)。
                # 因此 score_abs = s / (2/(k+1)) ∈ [0,1]，反映"命中强度"而非"相对排名"。
                max_possible = 2.0 / (_RRF_K + 1)
                out = []
                for s, eid in fused:
                    if eid not in eid_map:
                        continue
                    norm = min(s / max_possible, 1.0)
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
