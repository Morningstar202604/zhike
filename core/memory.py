"""长期记忆：按相关性召回 + 从访客消息自动沉淀偏好/事实。"""
from core import retrieval, store


def retrieve_relevant(query: str, mem: dict, top_k: int) -> list[tuple[str, str]]:
    if not mem:
        return []
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
    kws = mem_cfg.get("capture_keywords", ["喜欢", "常用", "偏好", "每次", "习惯", "总是"])
    hit_kws = [k for k in kws if k in query]
    if not hit_kws:
        return []
    key = "pref_" + hit_kws[0]
    store.set_memory(visitor_id, key, query[:60])
    return [key]
