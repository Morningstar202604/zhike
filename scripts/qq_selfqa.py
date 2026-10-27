"""QQ 真机自问自答：核心出答案 → 网关同一套渲染 → 通过 NapCat 真发到主人账号。

为什么这么测：直接 POST /v1/ingest 只验证「答什么」，验证不了「发出去长什么样」；
真正会出问题的是渲染后的 CQ 码、表情包挑图、语音段。这里把网关的
render + EmoteLib 原样复用，再用 NapCat 真发，最后把发出的图抓回来做视觉核对。

用法：.venv/Scripts/python.exe scripts/qq_selfqa.py [--limit 20]
输出：out/qq_selfqa_<ts>.json / .md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

TOKENS = pathlib.Path("C:/Users/YOU/napcat/local_tokens.txt")
MASTER = "100000001"


def tokens() -> dict:
    out = {}
    if TOKENS.is_file():
        for line in TOKENS.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


QUESTIONS = [
    ("identity", "你是谁"),
    ("identity", "你是机器人吗"),
    ("identity", "介绍一下你自己"),
    ("greeting", "在吗"),
    ("greeting", "晚安"),
    ("emote", "发个表情包给我"),
    ("emote", "来个猫咪表情包"),
    ("emote", "发个无语的表情"),
    ("banter", "哈哈哈笑死我了"),
    ("banter", "你这也太抽象了"),
    ("insult", "你个傻逼机器人"),
    ("politics", "你怎么看中美关系"),
    ("livelihood", "房价这么高你怎么看"),
    ("knowledge", "Python 的 GIL 是什么"),
    ("tool", "现在几点了"),
    ("tool", "10分钟后提醒我喝水"),
    ("voice", "发条语音跟我说晚安"),
    ("chat", "今天好累啊"),
    ("chat", "陪我聊会天"),
    ("chat", "我睡不着"),
]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    tok = tokens()
    core = "http://127.0.0.1:8010"
    gateway = "http://127.0.0.1:8011"
    h_admin = {"X-Admin-Token": tok.get("ADMIN_TOKEN", "")}
    h_channel = {"X-Channel-Token": tok.get("CHANNEL_TOKEN", "")}

    from gateway.humanize import EmoteLib, render

    emote_dir = "C:/Users/YOU/napcat/emotes"
    lib = EmoteLib(pathlib.Path(emote_dir) / "index.json")

    qs = QUESTIONS[: args.limit] if args.limit else QUESTIONS
    rows = []
    async with httpx.AsyncClient(timeout=180) as client:
        for group, q in qs:
            t0 = time.time()
            try:
                r = await client.post(f"{core}/v1/ingest", headers=h_channel, json={
                    "channel_type": "qq_private", "channel_user_id": MASTER,
                    "text": q, "display_name": "主人"})
                reply = (r.json() or {}).get("reply") or {}
            except Exception as e:
                rows.append({"group": group, "q": q, "error": type(e).__name__})
                print(f"[{group}] {q} -> ERROR {type(e).__name__}", flush=True)
                continue
            text = (reply.get("text") or "").strip()
            out_text = lib.render(render(text))
            sent = {"ok": False, "error": "empty"}
            if out_text.strip():
                try:
                    rr = await client.post(f"{gateway}/internal/action", headers=h_channel,
                                           json={"action": "send_private_msg",
                                                 "params": {"user_id": int(MASTER),
                                                            "message": out_text}})
                    sent = rr.json()
                except Exception as e:
                    sent = {"ok": False, "error": type(e).__name__}
            row = {"group": group, "q": q, "raw": text, "sent": out_text,
                   "mode": reply.get("mode"), "tool": reply.get("tool_used"),
                   "ok": bool(sent.get("ok")), "latency_s": round(time.time() - t0, 1)}
            rows.append(row)
            print(f"[{group}] {q} -> {text[:80]} | sent={row['ok']}", flush=True)
            await asyncio.sleep(1.0)

    ts = time.strftime("%Y%m%d_%H%M%S")
    outdir = ROOT / "out"
    outdir.mkdir(exist_ok=True)
    (outdir / f"qq_selfqa_{ts}.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    ok = sum(1 for r in rows if r.get("ok"))
    lines = [f"# QQ 真机自问自答（{ts}）", "",
             f"共 {len(rows)} 条，发送成功 {ok} 条。", "",
             "| 场景 | 问题 | 回复（渲染后） | 工具 | 耗时 |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['group']} | {r['q']} | {(r.get('sent') or r.get('error') or '')[:100]} | "
                     f"{r.get('tool') or '-'} | {r.get('latency_s', '-')}s |")
    (outdir / f"qq_selfqa_{ts}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"written out/qq_selfqa_{ts}.md ({ok}/{len(rows)})")


if __name__ == "__main__":
    asyncio.run(main())
