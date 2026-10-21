"""注入护栏：数据驱动正则规则（DB），全部停用时回退内置规则，护栏不失效。

输入侧：detect_injection（挡用户越狱）
输出侧：detect_output（挡 LLM 被攻破后泄露系统提示/越权内容）
"""
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

# 输出侧：LLM 回答里如果出现这些，说明系统提示被泄露，要拦截
_OUTPUT_PATTERNS = [
    re.compile(r"你是知客|你的系统提示|system prompt|你的指令是", re.IGNORECASE),
    re.compile(r"base_url|api_key|sk-[a-zA-Z0-9]{10,}", re.IGNORECASE),
    # "我被要求忽略/不能回答…"这类拒答话术：原文要求后面必须跟"回答/讨论/提及"，
    # 但模型常常只说"我被要求忽略这个问题"就结束，那样会漏网。
    # 这里放宽为：出现"我被要求/我被指示" + 限制性动词即命中。
    re.compile(r"我被(要求|指示|设定为?|告知).{0,10}(忽略|不(要|能|得|可)|禁止|拒绝|回避)", re.IGNORECASE),
    re.compile(r"(忽略|无视)(之前|以上|前面)的?(指令|要求|规则)", re.IGNORECASE),
]

_CACHE: tuple[int, list[tuple[re.Pattern, str]]] | None = None


def _active_rules() -> list[tuple[re.Pattern, str]]:
    """编译后的注入规则；按 DB 规则版本号缓存，规则 CRUD 后自动失效（多进程安全）。"""
    global _CACHE
    version = store.rules_version()
    if _CACHE is not None and _CACHE[0] == version:
        return _CACHE[1]
    patterns = []
    for r in store.list_injection_rules(enabled_only=True):
        try:
            patterns.append((re.compile(r["expr"], re.IGNORECASE), r["action"]))
        except re.error:
            continue
    if not patterns:
        for expr in _BUILTIN_PATTERNS:
            patterns.append((re.compile(expr, re.IGNORECASE), "block"))
    _CACHE = (version, patterns)
    return patterns


def detect_injection(text: str) -> tuple[str | None, str | None]:
    for pat, action in _active_rules():
        m = pat.search(text)
        if m:
            return m.group(0), action
    return None, None


def detect_output(text: str) -> str | None:
    """输出侧护栏：返回命中的违规片段（None 表示安全）。"""
    if not text:
        return None
    for pat in _OUTPUT_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)
    return None
