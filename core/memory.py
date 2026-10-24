"""长期记忆：按相关性召回 + 从访客消息自动沉淀偏好/事实。

召回两级：BGE 语义向量（sentence_transformers 可用时）→ bigram 词重叠兜底。
记忆条目通常几十条，实时编码开销毫秒级，无需额外索引。
"""
from core import retrieval, store

# cosine 低于该值视为不相关（bge-small-zh 短文本经验阈值）
_SEM_MIN_SCORE = 0.30


def retrieve_relevant(query: str, mem: dict, top_k: int) -> list[tuple[str, str]]:
    if not mem:
        return []
    # 语义优先：编码器可用时按向量相似度召回（中文同义改写也能命中）
    enc = retrieval._get_encoder() if retrieval.embedding_available() else None
    if enc is not None:
        try:
            items = list(mem.items())
            texts = [f"{k} {v}" for k, v in items]
            qv = enc.encode([query], normalize_embeddings=True)[0]
            vecs = enc.encode(texts, normalize_embeddings=True)
            scored = []
            for (k, v), vec in zip(items, vecs):
                score = float(qv @ vec)
                weight = 1.0 if "last_topic" not in k else 0.5
                if score >= _SEM_MIN_SCORE:
                    scored.append((score * weight, k, v))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [(k, v) for _, k, v in scored[:top_k]]
        except Exception:  # 编码失败退回词重叠
            pass
    # bigram 兜底（词重叠 × 权重）
    q_big = retrieval.bigrams(query)
    scored = []
    for k, v in mem.items():
        overlap = len(q_big & retrieval.bigrams(f"{k} {v}"))
        weight = 1.0 if "last_topic" not in k else 0.5
        if overlap > 0:
            scored.append((overlap * weight, k, v))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [(k, v) for _, k, v in scored[:top_k]]


def capture(query: str, visitor_id: str, cfg: dict) -> list[str]:
    mem_cfg = cfg.get("memory", {})
    if not mem_cfg.get("enable", True) or not mem_cfg.get("auto_capture", True) or not visitor_id:
        return []
    # 访客明确说过"别记我"：任何情况下都不再采集
    if store.memory_opted_out(visitor_id):
        return []
    kws = mem_cfg.get("capture_keywords", ["喜欢", "常用", "偏好", "每次", "习惯", "总是"])
    hit_kws = [k for k in kws if k in query]
    if not hit_kws:
        return []
    key = "pref_" + hit_kws[0]
    store.set_memory(visitor_id, key, query[:60])
    return [key]
