"""钩子（Hooks）：在对话管线的固定位置执行确定性规则。

定位与 ZCode/Claude Code 的 hooks 一致：**不是让模型自己判断，而是由系统强制执行**。
模型可能忘记加免责声明、忘记拦敏感词；钩子每轮必然执行。

支持的事件：
- `pre_input`：用户消息进管线之前。可 block（直接回固定话术）或 rewrite（改写后再处理）。
- `post_reply`：回复落库之前。可 append（追加固定尾巴）/ replace（整段替换）/ block（换成兜底话术）。

每条钩子：
  {id, name, event, match, action, value, enabled, times}
- match 是正则（不区分大小写）；空串表示对该事件的所有消息生效。
- times 是命中计数（运行时累加，便于在界面上看到「这条规则到底有没有生效」）。

安全约束：
- 正则长度上限 300 字符，且用 re.search 单次匹配，避免灾难性回溯（与注入规则同一策略）。
- 单次最多执行 50 条钩子，防止配置爆炸拖慢主链路。
- 任何钩子自身异常都只记日志、不阻断对话。
"""
from __future__ import annotations

import logging
import re
import uuid

logger = logging.getLogger("cs.events")

EVENTS = ("pre_input", "post_reply")
ACTIONS = {
    "pre_input": ("block", "rewrite"),
    "post_reply": ("append", "replace", "block"),
}
MAX_PATTERN = 300
MAX_HOOKS = 50

_DEFAULT_BLOCK = "这个话题我这边不方便展开，换个问题吧。"


def _compile(pattern: str):
    pattern = (pattern or "").strip()
    if not pattern:
        return None
    if len(pattern) > MAX_PATTERN:
        raise ValueError(f"正则过长（上限 {MAX_PATTERN} 字符）")
    return re.compile(pattern, re.IGNORECASE)


def normalize_items(items) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, dict) or len(out) >= MAX_HOOKS:
            continue
        event = str(raw.get("event") or "").strip()
        action = str(raw.get("action") or "").strip()
        if event not in EVENTS or action not in ACTIONS[event]:
            continue
        hid = str(raw.get("id") or "").strip() or f"hook-{uuid.uuid4().hex[:8]}"
        if hid in seen:
            continue
        match = str(raw.get("match") or "").strip()
        try:
            _compile(match)
        except (ValueError, re.error):
            continue  # 非法正则直接丢弃，避免一条坏配置废掉所有钩子
        seen.add(hid)
        out.append({
            "id": hid,
            "name": str(raw.get("name") or match or action)[:40],
            "event": event,
            "match": match[:MAX_PATTERN],
            "action": action,
            "value": str(raw.get("value") or "")[:2000],
            "enabled": False if raw.get("enabled") is False or raw.get("enabled") == 0 else True,
            "times": int(raw.get("times") or 0),
        })
    return out


def _hit(hook: dict, text: str) -> bool:
    try:
        pat = _compile(hook.get("match") or "")
    except (ValueError, re.error):
        return False
    if pat is None:
        return True
    return bool(pat.search(text or ""))


def run_pre_input(text: str, cfg: dict) -> dict:
    """返回 {action: 'pass'|'block'|'rewrite', text, hook}。

    只应用第一条命中的钩子（多条同时命中会让行为难以预测）。
    """
    for hook in normalize_items((cfg.get("hooks") or {}).get("items")):
        if not hook["enabled"] or hook["event"] != "pre_input":
            continue
        if not _hit(hook, text):
            continue
        if hook["action"] == "block":
            return {"action": "block", "text": hook["value"] or _DEFAULT_BLOCK, "hook": hook["name"]}
        replacement = hook["value"]
        try:
            rewritten = _compile(hook["match"]).sub(replacement, text, count=1) if hook["match"] else replacement
        except (ValueError, re.error):
            continue
        logger.info("hook pre_input rewrite: %s", hook["name"])
        return {"action": "rewrite", "text": rewritten or text, "hook": hook["name"]}
    return {"action": "pass", "text": text, "hook": ""}


def run_post_reply(text: str, cfg: dict) -> dict:
    """返回 {action: 'pass'|'append'|'replace'|'block', text, hook}。"""
    for hook in normalize_items((cfg.get("hooks") or {}).get("items")):
        if not hook["enabled"] or hook["event"] != "post_reply":
            continue
        if not _hit(hook, text):
            continue
        if hook["action"] == "append":
            return {"action": "append", "text": f"{text}\n\n{hook['value']}".strip(), "hook": hook["name"]}
        if hook["action"] == "replace":
            return {"action": "replace", "text": hook["value"], "hook": hook["name"]}
        return {"action": "block", "text": hook["value"] or _DEFAULT_BLOCK, "hook": hook["name"]}
    return {"action": "pass", "text": text, "hook": ""}
