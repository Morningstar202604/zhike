"""给表情包库里「还没有视觉标签」的图补标（增量、幂等）。

网关收到新梗图会重写 index.json（最多 300 条，最旧的被淘汰），所以打标必须
按 data 路径增量补，不能一次性写死。tags.json 只存 路径→中文标签。

用法：.venv/Scripts/python.exe scripts/tag_emotes.py [--workers 6]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from core import config  # noqa: E402

PROMPT = ("看这张表情包，用 3-6 个中文词描述：主体是什么（猫/狗/人/熊猫头/文字图等）、"
          "情绪（如 无语/爆笑/嘲讽/委屈/得意）、动作或文字内容。"
          "只输出词，用中文逗号分隔，不要句子、不要编号、不要解释。")


async def tag_one(client: httpx.AsyncClient, base: str, key: str, path: str,
                  sem: asyncio.Semaphore) -> str:
    ext = path.rsplit(".", 1)[-1].lower()
    mime = {"gif": "image/gif", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
    try:
        raw = pathlib.Path(path).read_bytes()
    except OSError:
        return ""
    if len(raw) > 5 * 1024 * 1024:
        return ""
    b64 = base64.b64encode(raw).decode()
    async with sem:
        for attempt in range(3):
            try:
                r = await client.post(f"{base}/chat/completions",
                                      headers={"Authorization": f"Bearer {key}"},
                                      json={"model": "u2-med", "messages": [{"role": "user", "content": [
                                          {"type": "text", "text": PROMPT},
                                          {"type": "image_url",
                                           "image_url": {"url": f"data:{mime};base64,{b64}"}}]}],
                                          "max_tokens": 60, "temperature": 0.1})
                if r.status_code == 200:
                    txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                    return txt.strip().replace("\n", " ")[:60]
                await asyncio.sleep(2 + attempt * 2)
            except Exception:
                await asyncio.sleep(2 + attempt * 2)
    return ""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    emote_dir = pathlib.Path("C:/Users/YOU/napcat/emotes")
    idx_file, tags_file = emote_dir / "index.json", emote_dir / "tags.json"
    items = json.loads(idx_file.read_text(encoding="utf-8")) if idx_file.is_file() else []
    tags = json.loads(tags_file.read_text(encoding="utf-8")) if tags_file.is_file() else {}
    todo = [str(i["data"]) for i in items if i.get("data") and not tags.get(str(i["data"]))]
    print(f"index={len(items)} tagged={len(tags)} todo={len(todo)}", flush=True)
    if not todo:
        return

    cfg = config.get_config()["llm"]
    sem = asyncio.Semaphore(args.workers)
    async with httpx.AsyncClient(timeout=120) as client:
        results = await asyncio.gather(*[tag_one(client, cfg["base_url"].rstrip("/"),
                                                 cfg["api_key"], p, sem) for p in todo])
    got = 0
    for p, t in zip(todo, results):
        if t:
            tags[p] = t
            got += 1
    # 清理已被淘汰的条目，避免 tags.json 无限增长
    live = {str(i["data"]) for i in items if i.get("data")}
    tags = {k: v for k, v in tags.items() if k in live}
    tags_file.write_text(json.dumps(tags, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"tagged {got}/{len(todo)}; tags.json={len(tags)}")


if __name__ == "__main__":
    asyncio.run(main())
