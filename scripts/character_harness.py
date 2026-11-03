"""角色一致性回归：全部角色 × 固定题库，离线判定，不依赖裁判模型。

## 和 persona_eval.py 的分工

``persona_eval.py`` 用裁判模型打分，适合比较"两版人设哪个更好"。
但它有两个限制，使它无法承担回归职责：

1. **要花模型调用**，几百条就是真金白银，没法进 CI；
2. **裁判提示词写死了「猫娘零」**（"被问身份时必须认得自己是猫娘"），
   评其他 14 个角色时会一律低分——结论不可用。

本脚本解决的是另一个问题：**角色有没有崩**。这件事不需要裁判，
用确定性的判据就能测：

- 每个角色都要能渲染出完整设定（事实/叙述/语料/未知策略齐全）
- 每个角色都要通过漂移检查（客服腔、身份漂移、禁忌词、口癖）
- 角色之间要有区分度（不能两个角色的提示词高度重合）
- 被问「你是谁」时，回复必须体现这个角色自己的身份锚点

## 用法

    python scripts/character_harness.py            # 只做静态检查，秒级，可进 CI
    python scripts/character_harness.py --live     # 额外跑真实模型（花钱、慢）
    python scripts/character_harness.py --live --only zero,tech

退出码：0 全过，1 有问题。CI 里直接跑不带 --live 的版本。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.character import load_all  # noqa: E402
from core.character_guard import check_reply  # noqa: E402
from core.personas import PRESETS  # noqa: E402

# 每个角色都会被问到的固定问题。选的都是"最容易暴露人设问题"的题：
# 身份题暴露自我认知，寒暄暴露语气，知识题暴露是否只会答话不会办事。
PROBES = [
    ("identity", "你是谁"),
    ("identity", "介绍一下你自己"),
    ("greeting", "在吗"),
    ("greeting", "早上好"),
    ("banter", "哈哈哈笑死我了"),
    ("knowledge", "开发票需要多久"),
    ("chat", "今天好累啊"),
]

# 两个角色的提示词相似度超过这个值就报警——说明是换皮不是新角色
SIMILARITY_WARN = 0.72


def _bigrams(text: str) -> set[str]:
    t = "".join(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def static_checks() -> tuple[list[str], list[str]]:
    """不花钱的检查：资料完整性、区分度、静态漂移。返回 (错误, 警告)。"""
    errors: list[str] = []
    warnings: list[str] = []

    cards = load_all()
    preset_ids = {p.id for p in PRESETS}

    # 1. 预设角色必须都有资料卡
    missing = preset_ids - set(cards)
    if missing:
        errors.append(f"这些预设角色还没有设定资料卡：{', '.join(sorted(missing))}")

    # 2. 资料卡不能有孤儿（定义在磁盘上但预设里没有）
    orphan = set(cards) - preset_ids
    if orphan:
        warnings.append(f"这些资料卡没有对应的预设角色：{', '.join(sorted(orphan))}")

    # 3. 每张卡的完整性
    for cid, card in cards.items():
        if not card.fields:
            errors.append(f"[{cid}] 没有结构化事实（fields 为空）")
        if len(card.narrative) < 5:
            errors.append(f"[{cid}] 叙述段落太少（{len(card.narrative)} 段，至少 5 段）")
        if not card.unknown_policy:
            errors.append(f"[{cid}] 没写「资料没有的事怎么办」")
        if len(card.voice_samples) < 3:
            errors.append(f"[{cid}] 参考语料不足（{len(card.voice_samples)} 条，至少 3 条）")
        rendered = card.render()
        if len(rendered) < 800:
            errors.append(f"[{cid}] 渲染后仅 {len(rendered)} 字，设定太薄")

    # 4. 角色之间要有区分度
    ids = sorted(cards)
    grams = {cid: _bigrams(cards[cid].render()) for cid in ids}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            sim = _jaccard(grams[a], grams[b])
            if sim >= SIMILARITY_WARN:
                warnings.append(f"[{a}] 与 [{b}] 设定相似度 {sim:.0%}，可能是换皮")

    # 5. 资料本身不能自带客服腔
    from core.character_guard import check_generic_assistant

    for cid, card in cards.items():
        # 只检查"角色说的话"部分（语料 + 叙述），不检查禁忌清单
        # ——禁忌清单里本来就会出现"不许说作为AI"这类字眼
        for sample in card.voice_samples:
            v = check_generic_assistant(sample)
            if v:
                errors.append(f"[{cid}] 语料里自带客服腔：{sample[:30]}")

    return errors, warnings


def static_drift_checks() -> list[str]:
    """用一组"典型坏回复"检验每个角色的守卫是否真的会报错。

    这是对**守卫本身**的测试，不是对模型的测试：如果守卫对客服腔都不报警，
    那它上线也拦不住任何东西。
    """
    errors = []
    cards = load_all()
    bad_reply = "作为AI助手，很高兴为您服务。希望这能帮到你。"
    for cid, card in cards.items():
        r = check_reply(bad_reply, card, strict_voice=False)
        if not r.violations:
            errors.append(f"[{cid}] 守卫没检出明显的客服腔")
    return errors


async def live_check(card, probes) -> list[str]:
    """真实模型跑一遍，检查回复是否守住人设。需要 API Key。"""
    from core import config, llm

    problems: list[str] = []
    cfg = config.get_config()
    llm_cfg = dict(cfg.get("llm") or {})
    llm_cfg["has_key"] = True

    for group, q in probes:
        ctx = {
            "persona": card.render(),
            "kb_block": "（无相关条目）",
            "tools_schema": [],
            "memory_block": "（暂无相关记忆）",
            "summary": "",
            "history": [],
            "modules_on": {"kb": False, "tools": False, "memory": False, "summary": False},
            "quote_block": "【用户这一句原话】" + q,
            "intent_block": "",
            "profile_block": "",
            "lexicon_block": "",
            "used": {},
        }
        try:
            data = await llm.react(q, ctx, {"llm": llm_cfg, "multimodal": {}})
        except Exception as e:  # noqa: BLE001
            problems.append(f"[{card.id}/{group}] 调用失败：{type(e).__name__}")
            continue
        answer = str((data or {}).get("answer") or "").strip()
        if not answer:
            problems.append(f"[{card.id}/{group}] 空回复")
            continue
        r = check_reply(answer, card, strict_voice=False)
        if not r.ok:
            kinds = ",".join(sorted(r.kinds))
            problems.append(f"[{card.id}/{group}] 漂移({kinds})：{answer[:40]}")
    return problems


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="额外跑真实模型（花钱、慢）")
    ap.add_argument("--only", default="", help="只测这些角色 id，逗号分隔")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = ap.parse_args()

    errors, warnings = static_checks()
    errors += static_drift_checks()

    live_problems: list[str] = []
    if args.live:
        cards = load_all()
        ids = [x.strip() for x in args.only.split(",") if x.strip()] or sorted(cards)
        for cid in ids:
            card = cards.get(cid)
            if card is None:
                errors.append(f"--only 指定的角色不存在：{cid}")
                continue
            print(f"[live] {cid} {card.name} ...", file=sys.stderr, flush=True)
            live_problems += await live_check(card, PROBES)

    result = {
        "errors": errors,
        "warnings": warnings,
        "live_problems": live_problems,
        "ok": not errors and not live_problems,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("=" * 60)
        print("角色一致性检查")
        print("=" * 60)
        cards = load_all()
        print(f"\n角色卡：{len(cards)} 个")
        print(f"预设角色：{len(PRESETS)} 个")
        if errors:
            print(f"\n[X] 错误 {len(errors)} 条：")
            for e in errors:
                print("   ", e)
        if warnings:
            print(f"\n[!] 警告 {len(warnings)} 条：")
            for w in warnings:
                print("   ", w)
        if live_problems:
            print(f"\n[!] 真机漂移 {len(live_problems)} 条：")
            for p in live_problems:
                print("   ", p)
        if not errors and not live_problems:
            print("\n[OK] 全部通过")
        print()

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
