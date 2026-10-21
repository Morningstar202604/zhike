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
    # 模型的推理过程（提示词要求输出，用于前端"思考中"折叠展示）
    thought: str = ""
    answer: str = ""
    citations: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
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
            return cls._lenient(data)

    @classmethod
    def _lenient(cls, data: dict) -> "LlmReply":
        """逐字段独立兜底：某个字段类型错不能把整条回答丢掉。

        之前整块 try/except 会导致 confidence="high" 时 answer 一起被清空，
        高质量回答被误判成空回复并转人工（已复现）。
        """
        answer = data.get("answer")
        answer = answer if isinstance(answer, str) else ("" if answer is None else str(answer))

        raw_cit = data.get("citations")
        if isinstance(raw_cit, (list, tuple)):
            citations = [str(x) for x in raw_cit if isinstance(x, (str, int))]
        elif isinstance(raw_cit, str) and raw_cit:
            citations = [raw_cit]          # 单个字符串不能被拆成字符列表
        else:
            citations = []

        try:
            confidence = float(data.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        tool = data.get("tool")
        tool_args = data.get("tool_args")
        return cls(
            thought=str(data.get("thought", "") or "")[:2000],
            answer=answer,
            citations=citations,
            confidence=confidence,
            escalate=bool(data.get("escalate", False)),
            escalate_reason=str(data.get("escalate_reason", "") or ""),
            tool=tool if isinstance(tool, str) else None,
            tool_args=tool_args if isinstance(tool_args, dict) else {},
        )
