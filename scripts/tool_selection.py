"""工具选择准确率评测：用户的一句话，能不能走到正确的那个工具。

为什么单独测：「工具注册了」不等于「工具能用」——最常坏的一环是模型压根不选工具，
或者选了个差不多的（要表情包却去 web_search）。

**测的是生产路径，不是裸模型**：管线里规则层（`tools.detect_tool` + 表情包直通）
先拦一道，拦不住的才交给模型。只测裸模型会把「规则已经处理好的」算成失败，
得出一个用户根本不会遇到的低分（第一版就这么错了：8/32）。

只判断落到哪个工具，不真的执行，所以便宜、可批量、不产生副作用。

分两类：
- must：这句话必须选到这个工具（确定性请求）
- never：这句话绝不能选这些工具（闲聊不该乱调工具）

用法：
    .venv/Scripts/python.exe scripts/tool_selection.py
输出：out/tool_selection_<ts>.json / .md
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

from core import config, llm as llm_mod, tools  # noqa: E402
from scripts.persona_eval import ctx_for  # noqa: E402

# (用户原话, 必须选中的工具, 说明)
MUST: list[tuple[str, str, str]] = [
    ("现在几点了", "now", "时间"),
    ("今天几号", "now", "日期"),
    ("算一下 128*37", "calc", "算式"),
    ("99 乘 99 等于多少", "calc", "算式"),
    ("发个表情包给我", "search_emote", "表情包"),
    ("来个猫咪表情包", "search_emote", "表情包"),
    ("10分钟后提醒我喝水", "remind", "提醒"),
    ("明天早上八点提醒我开会", "remind", "提醒"),
    ("帮我记住我喜欢喝美式", "remember", "记忆"),
    ("我上次说我喜欢喝什么", "recall_memory", "回忆"),
    ("列一下我的待办", "list_reminders", "待办"),
    ("发条语音说晚安", "send_voice", "语音"),
    ("给我画一只猫", "generate_image", "生图"),
    ("把这张图发群里", "send_image", "发图"),
    ("群里刚才在聊什么", "qq_group_history", "群记录"),
    ("群里有几个人", "qq_group_members", "群成员"),
    ("我加了哪些群", "qq_groups", "群列表"),
    ("我的好友都有谁", "qq_friends", "好友"),
    ("把张三禁言十分钟", "qq_group_ban", "群管理"),
    ("把群名改成摸鱼大队", "qq_group_rename", "群管理"),
    ("发个群公告说明天放假", "qq_group_notice", "群公告"),
    ("撤回刚才那条消息", "qq_recall", "撤回"),
    ("把这条设为群精华", "qq_essence", "精华"),
    ("戳一下他", "qq_poke", "互动"),
    ("给这条点个赞", "qq_emoji_like", "互动"),
    ("读一下 C:/tmp/a.txt", "read_file", "读文件"),
    ("列一下 D:/work 里有什么", "list_files", "列目录"),
    ("把这段写进 D:/work/note.md", "write_file", "写文件"),
    ("总结一下这个 PDF D:/a.pdf", "parse_document", "文档"),
    ("看一下这个链接 https://example.com 讲了什么", "read_webpage", "读网页"),
    ("帮我搜一下今天有什么科技新闻", "web_search", "搜索"),
    ("看看我刚发的这张图是什么", "describe_image", "识图"),
]

# 闲聊/知识类：绝不能调用这些「有副作用」的工具
NEVER: list[tuple[str, set[str], str]] = [
    ("今天好累啊", {"send_voice", "generate_image", "qq_send", "write_file", "remind"}, "闲聊"),
    ("你是谁", {"generate_image", "qq_send", "write_file"}, "身份"),
    ("你怎么看中美关系", {"qq_send", "write_file", "qq_group_ban"}, "政治"),
    ("哈哈哈笑死我了", {"write_file", "qq_group_ban", "remind"}, "玩梗"),
    ("Python 的 GIL 是什么", {"write_file", "qq_send", "generate_image"}, "知识"),
    ("我失恋了", {"generate_image", "write_file", "qq_group_ban"}, "情绪"),
    ("你好呀", {"generate_image", "write_file", "remind"}, "打招呼"),
]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=1,
                    help="u2-flash 4 并发就 82%% 429，默认串行")
    args = ap.parse_args()

    cfg = config.get_config()
    llm_cfg = cfg["llm"]
    models = [{"model": llm_cfg["model"], "base": llm_cfg["base_url"].rstrip("/"),
               "key": llm_cfg["api_key"]}]
    for p in llm_cfg.get("providers") or []:
        if p.get("model") and p.get("api_key") and p["model"] != llm_cfg["model"]:
            models.append({"model": p["model"], "base": p["base_url"].rstrip("/"), "key": p["api_key"]})
    models = [m for m in models if m["key"]]
    schema = tools.tool_schema_block(cfg)
    names = [t["name"] for t in schema]
    print(f"tools={len(schema)}", flush=True)

    # 直接用生产路径 llm.react：它内部就是原生 function calling + 多模型备援 + 429 冷却，
    # 自己再写一套请求循环等于测一个用户根本不会经过的路径（而且容易被 429 卡死）。
    sem = asyncio.Semaphore(args.workers)

    async def pick(q: str) -> tuple[str, str]:
        """返回 (实际落到的工具, 证据)。先规则层，再模型层，与生产一致。"""
        from core import intent as intent_mod

        tools_map = {t["name"]: True for t in schema}
        # 1) 规则层：确定性问法直接路由（提醒/时间/算数/读文件/看群记录…）
        rule_name, _args = tools.detect_tool(q, tools_map)
        if rule_name:
            return rule_name, "rule"
        # 2) 表情包直通：意图命中就由网关挑图，不需要模型选工具
        if intent_mod.classify(q).name == intent_mod.INTENT_EMOTE and "search_emote" in tools_map:
            return "search_emote", "intent"
        # 3) 模型层：原生 function calling
        ctx = ctx_for(cfg["context"]["persona"], q)
        ctx["tools_schema"] = schema
        ctx["tools"] = tools_map
        ctx["modules_on"] = {"kb": False, "tools": True, "memory": False, "summary": False}
        err = "no data"
        for attempt in range(3):
            async with sem:
                try:
                    data = await asyncio.wait_for(llm_mod.react(q, ctx, cfg), timeout=180)
                except Exception as e:
                    data = None
                    err = f"ERR {type(e).__name__}"
                else:
                    err = "no data"
            if data:
                return str(data.get("tool") or ""), str(data.get("answer") or "")[:120]
            await asyncio.sleep(4 + attempt * 6)
        return "", err

    async def one_must(item: tuple[str, str, str]) -> dict:
        q, want, tag = item
        got, raw = await pick(q)
        return {"kind": "must", "q": q, "want": want, "got": got, "tag": tag,
                "ok": got == want, "raw": raw}

    async def one_never(item: tuple[str, set[str], str]) -> dict:
        q, banned, tag = item
        got, raw = await pick(q)
        return {"kind": "never", "q": q, "want": "-", "got": got, "tag": tag,
                "ok": got not in banned, "raw": raw}

    # 逐条落盘 + 实时打印：39 个工具的选择测试会被 429/端点抖动拖到几十分钟，
    # 一次性 gather 后写文件等于中途什么都看不到（日志只有 "tools=39"）。
    outdir = ROOT / "out"
    outdir.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    jsonl = outdir / f"tool_selection_{ts}.jsonl"
    results: list[dict] = []

    async def run_and_log(coro, label: str):
        r = await coro
        results.append(r)
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + chr(10))
        print(f"[{len(results)}/{len(MUST) + len(NEVER)}] {label} "
              f"{'OK' if r['ok'] else 'FAIL'} want={r['want']} got={r['got'] or '-'}", flush=True)
        return r

    await asyncio.gather(*[run_and_log(one_must(i), i[0]) for i in MUST],
                         *[run_and_log(one_never(i), i[0]) for i in NEVER])

    (outdir / f"tool_selection_{ts}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    must = [r for r in results if r["kind"] == "must"]
    never = [r for r in results if r["kind"] == "never"]
    m_ok = sum(1 for r in must if r["ok"])
    n_ok = sum(1 for r in never if r["ok"])

    lines = [f"# 工具选择评测（{ts}）", "",
             f"工具池 {len(names)} 个；模型 {" | ".join(f'{m["model"]}' for m in models)}。", "",
             "| 类别 | 通过 | 合计 | 准确率 |", "|---|---|---|---|",
             f"| 必须选中（must） | {m_ok} | {len(must)} | {m_ok / len(must):.1%} |",
             f"| 不能乱选（never） | {n_ok} | {len(never)} | {n_ok / len(never):.1%} |", "",
             "## 选错的（must）", "", "| 用户原话 | 应选 | 实际 | 场景 |", "|---|---|---|---|"]
    for r in must:
        if not r["ok"]:
            lines.append(f"| {r['q']} | {r['want']} | {r['got'] or '（没选工具）'} | {r['tag']} |")
    bad_never = [r for r in never if not r["ok"]]
    if bad_never:
        lines += ["", "## 乱调工具的（never）", "", "| 用户原话 | 实际调了 |", "|---|---|"]
        for r in bad_never:
            lines.append(f"| {r['q']} | {r['got']} |")
    (outdir / f"tool_selection_{ts}.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nwritten out/tool_selection_{ts}.md")


if __name__ == "__main__":
    asyncio.run(main())
