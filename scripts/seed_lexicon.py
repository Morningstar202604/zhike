"""黑话库种子：把高频网梗/群黑话写进 lexicon（幂等，重复跑只更新释义）。

为什么放脚本而不是硬编码：lexicon 是运行时数据（用户还能自己加），
种子脚本只是「第一次部署时把常见的填上」，不该写进代码默认值。

用法：.venv/Scripts/python.exe scripts/seed_lexicon.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import profiles  # noqa: E402

SEED: list[tuple[str, str]] = [
    ("典", "太典型了，用来嘲讽对方发言套路化"),
    ("急", "你急了，指对方情绪失控"),
    ("绷不住", "没忍住笑出来 / 情绪破防"),
    ("破防", "心理防线被戳穿"),
    ("小丑", "自以为在演主角、实际很可笑的人"),
    ("抽象", "离谱到无法理解，也可指整活"),
    ("就这", "期望落空的轻蔑"),
    ("来点活", "让对方整点有意思的"),
    ("整活", "搞事情、制造节目效果"),
    ("6", "牛逼 / 无语的反讽，看语境"),
    ("典中典", "典型中的典型，加强版嘲讽"),
    ("乐", "乐子、看热闹"),
    ("草", "笑死（谐音），不是植物"),
    ("awsl", "啊我死了，表示被可爱到"),
    ("yyds", "永远的神"),
    ("绝绝子", "太绝了（多为反讽用法）"),
    ("牛马", "被使唤的打工人"),
    ("班味", "上班上出来的疲惫气质"),
    ("摸鱼", "上班偷懒"),
    ("内卷", "无意义的内耗竞争"),
    ("躺平", "退出竞争、不再挣扎"),
    ("emo", "情绪低落"),
    ("社死", "社会性死亡，当众丢脸"),
    ("吃瓜", "围观八卦"),
    ("整挺好", "反讽：搞得一团糟还嘴硬"),
]


def main() -> None:
    added = 0
    for term, meaning in SEED:
        if profiles.add_lexicon("global", term, meaning=meaning):
            added += 1
    print(f"lexicon seeded: {added}/{len(SEED)}, total={len(profiles.list_lexicon('', 500))}")


if __name__ == "__main__":
    main()
