"""敏感信息打码：审计日志、工具结果、错误信息回传前统一过一遍。

原则：
- 参数名命中敏感词 → 值整体替换（不看内容长度）
- 值命中已知令牌特征（sk-/ghp_/Bearer/长十六进制）→ 局部替换
- 文件路径只保留路径，不保留内容
"""
from __future__ import annotations

import re
from typing import Any

# 参数名（不区分大小写）命中即视为敏感
_SENSITIVE_KEYS = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|pwd|authorization|cookie|session[_-]?secret|private[_-]?key)",
    re.IGNORECASE,
)

# 值里的令牌特征
_VALUE_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{10,}", re.IGNORECASE),
    # 长十六进制串（密钥/摘要）。要求大小写混合或含数字，避免把一整串相同字母的
    # 普通文本（如 "AAAA..."）误判成密钥；同时限制上界减少误报。
    re.compile(r"\b(?=[A-Fa-f0-9]*[0-9])(?=[A-Fa-f0-9]*[A-Fa-f])[A-Fa-f0-9]{32,128}\b"),
]

_MASK = "***"
# 递归深度上限：防止畸形数据把栈打爆
_MAX_DEPTH = 8
# 单值最大保留长度：超长内容（如图片 base64）不整体写入审计
_MAX_VALUE_LEN = 512


def mask_text(text: str) -> str:
    if not text:
        return text
    out = text
    for pat in _VALUE_PATTERNS:
        out = pat.sub(_MASK, out)
    return out


def mask_value(value: Any, depth: int = 0) -> Any:
    """递归打码任意结构。超长字符串截断（图片 base64 之类不该进审计）。"""
    if depth > _MAX_DEPTH:
        return "<too-deep>"
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if isinstance(k, str) and _SENSITIVE_KEYS.search(k):
                result[k] = _MASK if v not in (None, "") else v
            else:
                result[k] = mask_value(v, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        # 只保留前 20 项，避免大数组塞爆审计
        return [mask_value(v, depth + 1) for v in list(value)[:20]]
    if isinstance(value, str):
        masked = mask_text(value)
        if len(masked) > _MAX_VALUE_LEN:
            return masked[:_MAX_VALUE_LEN] + f"...(truncated, total {len(masked)})"
        return masked
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return mask_text(str(value))


def summarize_for_approval(action: str, params: dict, spec_summary: str = "") -> str:
    """给人工确认看的摘要：人话描述 + 关键参数（已打码）。"""
    safe = mask_value(params or {})
    parts = [spec_summary or action]
    for k, v in list(safe.items())[:6]:
        if isinstance(v, str) and len(v) > 80:
            v = v[:80] + "…"
        parts.append(f"{k}={v}")
    return f"{parts[0]}（{', '.join(parts[1:])}）" if len(parts) > 1 else parts[0]
