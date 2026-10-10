"""知识库检索：bigram 词重叠打分（零依赖兜底）+ 可选向量语义检索（sentence-transformers + BGE 中文小模型）。

向量后端为可选依赖（requirements-embedding.txt），未安装或加载失败时自动降级为纯 bigram，不影响可用性。
两路分数取 max 混合：bigram 保字面精确命中，BGE 余弦保语义泛化。
"""
import json
import pathlib
import re

KB_PATH = pathlib.Path(__file__).parent / "data" / "kb.json"


_KB_CACHE: tuple[str, int, list[dict]] | None = None


def load_kb() -> list[dict]:
    """知识库条目；按「路径 + mtime」缓存，文件被改写后自动重载。路径入键防多文件串缓存。"""
    global _KB_CACHE
    mtime = KB_PATH.stat().st_mtime_ns
    key = str(KB_PATH)
    if _KB_CACHE is None or _KB_CACHE[0] != key or _KB_CACHE[1] != mtime:
        with open(KB_PATH, encoding="utf-8") as f:
            _KB_CACHE = (key, mtime, json.load(f)["kb"])
    return _KB_CACHE[2]


def bigrams(text: str) -> set[str]:
    cjk = [ch for ch in text.lower() if "\u4e00" <= ch <= "\u9fff"]
    pairs = {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}
    return pairs | set(re.findall(r"[a-z0-9]{2,}", text.lower()))


# ---- 向量后端（成熟方案：BAAI/bge-small-zh-v1.5，开源中文检索事实标准，CPU 可跑）----
BGE_MODEL = "BAAI/bge-small-zh-v1.5"
# BGE-zh 余弦经验分界：无关 ≈<0.55，强相关 ≈>0.9；线性映射到 [0,1] 与 bigram 分数同刻度
_COS_FLOOR, _COS_CEIL = 0.55, 0.90
_EMB_STATE: dict = {"model": None, "matrix": None, "ids": [], "sig": "", "failed": False}
_BGE_QUERY_PREFIX = "为这个句子生成表示以检索相关文章："  # BGE-zh 官方查询指令


def embedding_available() -> bool:
    """向量后端依赖是否可导入（find_spec 不触发模型下载/加载，供控制台状态展示与 auto 判断预检）。"""
    import importlib.util

    return importlib.util.find_spec("sentence_transformers") is not None


def _get_encoder():
    """惰性加载 BGE 编码器；未安装依赖或加载失败返回 None（进程内只试一次，不反复拖慢请求）。"""
    if _EMB_STATE["failed"]:
        return None
    if _EMB_STATE["model"] is None:
        try:
            from sentence_transformers import SentenceTransformer  # 可选依赖

            _EMB_STATE["model"] = SentenceTransformer(BGE_MODEL)
        except Exception as e:  # 未安装 / 模型下载失败 / 内存不足
            _EMB_STATE["failed"] = True
            import logging

            logging.getLogger("cs.events").info("embedding backend unavailable, bigram only: %s", str(e)[:80])
            return None
    return _EMB_STATE["model"]


def _emb_scores(query: str, kb: list[dict]) -> dict[str, float] | None:
    """query × 全库余弦相似度 → {entry_id: 0~1 校准分}；向量索引随 KB 文件 mtime 自动重建。"""
    if not kb or _get_encoder() is None:
        return None
    import numpy as np

    sig = f"{KB_PATH}:{KB_PATH.stat().st_mtime_ns}"
    if _EMB_STATE["sig"] != sig or _EMB_STATE["matrix"] is None:
        docs = [f"{e['title']} {e['text']} {' '.join(e.get('tags', []))}" for e in kb]
        mat = _EMB_STATE["model"].encode(docs, normalize_embeddings=True)
        _EMB_STATE["matrix"] = np.asarray(mat, dtype="float32")
        _EMB_STATE["ids"] = [e["id"] for e in kb]
        _EMB_STATE["sig"] = sig
    qv = _EMB_STATE["model"].encode([_BGE_QUERY_PREFIX + query], normalize_embeddings=True)[0]
    sims = _EMB_STATE["matrix"] @ np.asarray(qv, dtype="float32")
    span = _COS_CEIL - _COS_FLOOR
    return {eid: max(0.0, min(1.0, (float(s) - _COS_FLOOR) / span)) for eid, s in zip(_EMB_STATE["ids"], sims)}


def retrieve_scored(query: str, top_k: int, min_overlap: float, use_embedding: bool | None = None) -> list[tuple[float, dict]]:
    """混合检索：score = max(bigram 词重叠, BGE 余弦校准分)。

    use_embedding: None=自动（依赖可用即用）/ False=强制纯 bigram / True=要求向量后端（不可用则退回 bigram）。
    """
    kb = load_kb()
    q_big = bigrams(query)
    emb: dict[str, float] | None = None
    if use_embedding is not False:
        try:
            emb = _emb_scores(query, kb)
        except Exception:
            emb = None
    scored = []
    for entry in kb:
        doc_text = f"{entry['text']} {entry['title']} {' '.join(entry.get('tags', []))}"
        overlap = len(q_big & bigrams(doc_text))
        score = overlap / max(len(q_big), 1) if overlap else 0.0
        if emb:
            score = max(score, emb.get(entry["id"], 0.0))
        if score <= 0 or score < min_overlap:
            continue
        scored.append((score, entry))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_k]


def retrieve(query: str, top_k: int = 4, min_overlap: float = 0.0, use_embedding: bool | None = None) -> list[dict]:
    return [e for _, e in retrieve_scored(query, top_k, min_overlap, use_embedding)]
