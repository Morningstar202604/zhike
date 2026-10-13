"""LLM 输出结构化 schema（借鉴 OpenAI Agents SDK / PydanticAI 的结构化输出范式）。

不引入 instructor 等重依赖：用 pydantic 模型对 LLM 返回的 dict 做一次校验，
缺字段/类型错时给安全默认值，避免裸 json.loads 后 KeyError/TypeError 把管线打崩。
"""
from pydantic import BaseModel, Field, ValidationError
from typing import Any


class LlmToolCall(BaseModel):
    name: str = ""
    args: dict[str, Any] = Field(default_factory=dict)


class LlmReply(BaseModel):
    """LLM 必须返回的 JSON schema。所有字段都给安全默认值，校验失败也能降级。"""
    answer: str = ""
    citations: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    escalate: bool = False
    escalate_reason: str = ""
    tool: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def safe_parse(cls, data: dict | None) -> "LlmReply":
        """宽松解析：任何校验错误都返回一个低置信度空回复，绝不抛异常。"""
        if not isinstance(data, dict):
            return cls()
        try:
            return cls.model_validate(data)
        except ValidationError:
            # 二次宽松：逐字段取，类型错就用默认值
            try:
                return cls(
                    answer=str(data.get("answer", "") or ""),
                    citations=[str(x) for x in (data.get("citations") or []) if isinstance(x, (str, int))],
                    confidence=float(data.get("confidence", 0.0) or 0.0),
                    escalate=bool(data.get("escalate", False)),
                    escalate_reason=str(data.get("escalate_reason", "") or ""),
                    tool=data.get("tool") if isinstance(data.get("tool"), str) else None,
                    tool_args=data.get("tool_args") if isinstance(data.get("tool_args"), dict) else {},
                )
            except Exception:
                return cls()
