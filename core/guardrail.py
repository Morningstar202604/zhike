"""注入护栏：数据驱动正则规则（DB），全部停用时回退内置规则，护栏不失效。"""
import re

from core import store

_BUILTIN_PATTERNS = [
    r"忽略(之前|所有|以上)的?(指令|规则|设定|系统提示)",
    r"你现在是(一个|一名)?\s*(不受限制的|无限制的)",
    r"reveal (the )?(system )?(prompt|instructions)",
    r"忽略.{0,10}(指令|提示|规则)",
    r"pretend (you are|to be) (a )?(new|unrestricted)",
    r"disregard (all|the) (previous|prior)",
    r"jailbreak|越狱",
]


def _active_rules() -> list[tuple[re.Pattern, str]]:
    patterns = []
    for r in store.list_injection_rules(enabled_only=True):
        try:
            patterns.append((re.compile(r["expr"], re.IGNORECASE), r["action"]))
        except re.error:
            continue
    if not patterns:
        for expr in _BUILTIN_PATTERNS:
            patterns.append((re.compile(expr, re.IGNORECASE), "block"))
    return patterns


def detect_injection(text: str) -> tuple[str | None, str | None]:
    for pat, action in _active_rules():
        m = pat.search(text)
        if m:
            return m.group(0), action
    return None, None
