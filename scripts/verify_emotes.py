"""把最近发出去的表情包按关键词核对：下载 → 视觉识别 → 与请求词比对。

为什么要闭环：命中率只看 tags.json 是自欺欺人——真正要验证的是
「用户要猫，QQ 里收到的那张图是不是猫」。这里把 QQ 服务器上的图下载回来，
再用视觉模型独立描述一遍，和请求词做语义对照。

用法：.venv/Scripts/python.exe scripts/verify_emotes.py [--count 20]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from core import config  # noqa: E402

NAPCAT_HOME = pathlib.Path(os.environ.get("NAPCAT_HOME") or pathlib.Path.home() / "napcat")
TOKENS = NAPCAT_HOME / "local_tokens.txt"
MASTER = (os.environ.get("AUTHORIZED_QQ") or "").split(",")[0].strip()
EMOTE_DIR = NAPCAT_HOME / "emotes"


def tokens() -> dict:
    out = {}
    if TOKENS.is_file():
        for line in TOKENS.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


async def describe(client: httpx.AsyncClient, b64: bytes) -> str:
    full = config.get_config()
    cfg = full["llm"]
    vision = (full.get("multimodal") or {}).get("vision_model") or "u2-med"
    data = base64.b64encode(b64).decode()
    mime = "image/gif" if b64[:3] == b"GIF" else ("image/png" if b64[:4] == b"\x89PNG" else "image/jpeg")
    try:
        r = await client.post(f"{cfg['base_url'].rstrip('/')}/chat/completions",
                              headers={"Authorization": f"Bearer {cfg['api_key']}"},
                              json={"model": vision, "messages": [{"role": "user", "content": [
                                  {"type": "text", "text": "用不超过 8 个中文词描述这张图：主体、情绪、动作或文字。只给词。"},
                                  {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}]}],
                                  "max_tokens": 60, "temperature": 0.1})
        if r.status_code == 200:
            return ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
    except Exception:
        pass
    return ""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=12)
    args = ap.parse_args()
    tok = tokens()
    tags = json.loads((EMOTE_DIR / "tags.json").read_text(encoding="utf-8"))
    index = json.loads((EMOTE_DIR / "index.json").read_text(encoding="utf-8"))
    by_hash = {}
    for it in index:
        try:
            by_hash[hashlib.md5(pathlib.Path(it["data"]).read_bytes()).hexdigest()] = it["data"]
        except OSError:
            pass

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post("http://127.0.0.1:8011/internal/action",
                              headers={"X-Channel-Token": tok["CHANNEL_TOKEN"]},
                              json={"action": "get_friend_msg_history",
                                    "params": {"user_id": int(MASTER), "count": args.count}})
        msgs = r.json()["data"]["messages"]
        rows = []
        for m in msgs:
            text = "".join(s["data"].get("text", "") for s in m.get("message", [])
                           if s["type"] == "text")
            imgs = [s for s in m.get("message", []) if s["type"] == "image"]
            if not imgs:
                continue
            url = imgs[0]["data"].get("url", "")
            if not url:
                continue
            try:
                raw = (await client.get(url)).content
            except Exception:
                continue
            src = by_hash.get(hashlib.md5(raw).hexdigest(), "")
            seen = await describe(client, raw)
            rows.append({"text": text[:30], "file": pathlib.Path(src).name if src else "?",
                         "库内标签": tags.get(src, "?"), "视觉复检": seen, "bytes": len(raw)})
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        outdir = ROOT / "out"
        outdir.mkdir(exist_ok=True)
        import time
        (outdir / f"emote_verify_{time.strftime('%Y%m%d_%H%M%S')}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
