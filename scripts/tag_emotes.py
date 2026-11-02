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
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from core import config  # noqa: E402

PROMPT = ("看这张表情包，用 3-6 个中文词描述：主体是什么（猫/狗/人/熊猫头/文字图等）、"
          "情绪（如 无语/爆笑/嘲讽/委屈/得意）、动作或文字内容。"
          "只输出词，用中文逗号分隔，不要句子、不要编号、不要解释。")


async def tag_one(client: httpx.AsyncClient, base: str, key: str, model: str, path: str,
                  sem: asyncio.Semaphore, taggers: list[tuple[str, str, str]] | None = None) -> str:
    """给单张图打标。

    ``taggers`` 是 ``[(base_url, api_key, model), ...]`` 的候选链：主模型 429 /
    模型不存在时自动换下一个。硬编码模型名曾是本脚本的真实缺陷——端点换了
    就直接 404，整批图永远打不上标。
    """
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
    chain = taggers or [(base, key, model)]
    async with sem:
        for b, k, m in chain:
            for attempt in range(2):
                try:
                    r = await client.post(f"{b.rstrip('/')}/chat/completions",
                                          headers={"Authorization": f"Bearer {k}"},
                                          json={"model": m, "messages": [{"role": "user", "content": [
                                              {"type": "text", "text": PROMPT},
                                              {"type": "image_url",
                                               "image_url": {"url": f"data:{mime};base64,{b64}"}}]}],
                                              "max_tokens": 60, "temperature": 0.1})
                    if r.status_code == 200:
                        txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                        txt = txt.strip().replace("\n", " ")[:60]
                        if txt:
                            return txt
                        break  # 空内容换下一个候选
                    if r.status_code in (400, 404):
                        break  # 模型不存在/不支持图片：换下一个候选，别浪费重试
                    await asyncio.sleep(2 + attempt * 2)
                except Exception:
                    await asyncio.sleep(2 + attempt * 2)
    return ""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    emote_dir = pathlib.Path(os.environ.get("GATEWAY_EMOTE_DIR")
                             or pathlib.Path.home() / "napcat" / "emotes")
    idx_file, tags_file = emote_dir / "index.json", emote_dir / "tags.json"
    items = json.loads(idx_file.read_text(encoding="utf-8")) if idx_file.is_file() else []
    tags = json.loads(tags_file.read_text(encoding="utf-8")) if tags_file.is_file() else {}
    todo = [str(i["data"]) for i in items if i.get("data") and not tags.get(str(i["data"]))]
    print(f"index={len(items)} tagged={len(tags)} todo={len(todo)}", flush=True)
    if not todo:
        return

    cfg = config.get_config()["llm"]
    # 候选链：主模型优先，随后是配置里的备援厂商（都用各自的 base_url / key）。
    taggers: list[tuple[str, str, str]] = []
    if cfg.get("base_url") and cfg.get("api_key"):
        taggers.append((cfg["base_url"], cfg["api_key"], cfg.get("model") or "gpt-4o-mini"))
    for prov in cfg.get("providers") or []:
        if prov.get("base_url") and prov.get("api_key") and prov.get("model"):
            taggers.append((prov["base_url"], prov["api_key"], prov["model"]))
    if not taggers:
        print("未配置可用的模型厂商，无法打标")
        return
    print("taggers:", [f"{m}@{b}" for b, _, m in taggers], flush=True)

    sem = asyncio.Semaphore(args.workers)
    got = 0
    done = 0
    lock = asyncio.Lock()

    def _flush() -> None:
        """增量落盘：每打标一批就写一次。

        早期版本把 300 张全 gather 完才写一次，中途超时/断网就整批丢失——
        大图库跑一次要几分钟，这种"全或无"写盘等于没有进度。
        """
        live = {str(i["data"]) for i in items if i.get("data")}
        kept = {k: v for k, v in tags.items() if k in live}
        tags_file.write_text(json.dumps(kept, ensure_ascii=False, indent=0), encoding="utf-8")

    async def run_one(p: str) -> None:
        nonlocal got, done
        text = await tag_one(client, taggers[0][0], taggers[0][1], taggers[0][2], p, sem, taggers)
        async with lock:
            done += 1
            if text:
                tags[p] = text
                got += 1
            if done % 20 == 0:
                _flush()
                print(f"  progress {done}/{len(todo)} tagged={got}", flush=True)

    async with httpx.AsyncClient(timeout=120) as client:
        await asyncio.gather(*[run_one(p) for p in todo])

    _flush()
    print(f"tagged {got}/{len(todo)}; tags.json={len(tags)}")


if __name__ == "__main__":
    asyncio.run(main())
