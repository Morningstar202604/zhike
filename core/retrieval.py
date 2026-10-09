"""知识库检索：bigram 词重叠打分，零外部依赖；可替换为 embedding 实现。"""
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


def retrieve_scored(query: str, top_k: int, min_overlap: float) -> list[tuple[float, dict]]:
    kb = load_kb()
    q_big = bigrams(query)
    scored = []
    for entry in kb:
        doc_text = f"{entry['text']} {entry['title']} {' '.join(entry.get('tags', []))}"
        overlap = len(q_big & bigrams(doc_text))
        if overlap == 0:
            continue
        score = overlap / max(len(q_big), 1)
        if score < min_overlap:
            continue
        scored.append((score, entry))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_k]


def retrieve(query: str, top_k: int = 4, min_overlap: float = 0.0) -> list[dict]:
    return [e for _, e in retrieve_scored(query, top_k, min_overlap)]
