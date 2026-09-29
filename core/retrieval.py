"""知识库检索：bigram 词重叠打分，零外部依赖；可替换为 embedding 实现。"""
import json
import pathlib
import re

KB_PATH = pathlib.Path(__file__).parent / "data" / "kb.json"


def load_kb() -> list[dict]:
    with open(KB_PATH, encoding="utf-8") as f:
        return json.load(f)["kb"]


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
