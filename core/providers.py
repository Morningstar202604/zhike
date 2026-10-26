"""模型接入：厂商预设、模型发现、能力自动匹配、连通性测试。

只支持三种主流协议（不做厂商私有 SDK 适配）：
- openai-compatible：GET /models + POST /chat/completions（DeepSeek/GLM/Qwen/Kimi/豆包/自建网关）
- anthropic-messages：GET /models + POST /messages
- google-gemini：GET /models?key= + POST /models/{model}:generateContent

设计要点：
- 模型列表**只来自端点真实返回**，不内置假列表（返回空就明说，让用户手填）。
- 能力（上下文/输出/工具调用）优先取端点上报值，缺失时按模型家族保守推断，
  并标注 source=auto|reported，前端可覆盖。
- 所有出站请求走同一套 SSRF 守卫（url_guard），内网端点需显式 private_ok。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

PROTOCOLS = ("openai-compatible", "anthropic-messages", "google-gemini")

PROTOCOL_LABEL = {
    "openai-compatible": "OpenAI 兼容（Chat Completions）",
    "anthropic-messages": "Anthropic Messages",
    "google-gemini": "Google Gemini",
}


@dataclass(frozen=True)
class Preset:
    name: str
    base_url: str
    protocol: str = "openai-compatible"
    # 本地/内网端点需要显式放行，云元数据地址仍会被 url_guard 拒绝
    private_ok: bool = False
    note: str = ""


# 只放「地址 + 协议」，模型一律由端点探测给出（不预置模型名，避免过期数据误导）
PRESETS: list[Preset] = [
    Preset("OpenAI", "https://api.openai.com/v1"),
    Preset("Anthropic Claude", "https://api.anthropic.com/v1", "anthropic-messages"),
    Preset("Google Gemini", "https://generativelanguage.googleapis.com/v1beta", "google-gemini"),
    Preset("DeepSeek", "https://api.deepseek.com/v1"),
    Preset("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4"),
    Preset("通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    Preset("Kimi", "https://api.moonshot.cn/v1"),
    Preset("豆包·火山方舟", "https://ark.cn-beijing.volces.com/api/v3"),
    Preset("UniSound MaaS", "https://maas-api.unisound.com/v1"),
    Preset("SiliconFlow", "https://api.siliconflow.cn/v1"),
    Preset("Ollama 本地", "http://localhost:11434/v1", private_ok=True,
           note="本机模型，需勾选「本地/内网端点」"),
]


@dataclass
class ModelInfo:
    id: str
    display_name: str = ""
    context_window: int = 0
    max_output: int = 0
    tool_calling: bool | None = None
    source: str = "reported"          # reported | auto | unknown

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name or self.id,
            "context_window": self.context_window,
            "max_output": self.max_output,
            "tool_calling": self.tool_calling,
            "source": self.source,
        }


# 模型家族 → (上下文, 单次输出, 是否支持工具调用)。仅在上游不报值时使用。
# 数值取各厂商公开的保守下限，宁可低估也不虚报（虚报会让长文请求被截断）。
_FAMILY_HINTS: list[tuple[re.Pattern, int, int, bool]] = [
    (re.compile(r"claude", re.I), 200_000, 8_192, True),
    (re.compile(r"gemini", re.I), 1_000_000, 8_192, True),
    (re.compile(r"gpt-4\.1|gpt-4o|o3|o4", re.I), 128_000, 16_384, True),
    (re.compile(r"gpt-3\.5|gpt-4(?!\d)", re.I), 8_192, 4_096, True),
    (re.compile(r"deepseek", re.I), 65_536, 8_192, True),
    (re.compile(r"glm-4|glm-5", re.I), 128_000, 4_096, True),
    (re.compile(r"qwen", re.I), 32_768, 8_192, True),
    (re.compile(r"kimi|moonshot", re.I), 128_000, 8_192, True),
    (re.compile(r"doubao|seed", re.I), 128_000, 4_096, True),
    (re.compile(r"u2", re.I), 128_000, 8_192, True),
    (re.compile(r"llama|mistral|qwen", re.I), 32_768, 4_096, True),
]


def infer_capabilities(model_id: str) -> tuple[int, int, bool | None]:
    for pat, ctx, out, tools in _FAMILY_HINTS:
        if pat.search(model_id or ""):
            return ctx, out, tools
    return 0, 0, None


def _as_int(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _is_embedding_or_media(model_id: str) -> bool:
    """过滤非聊天模型：嵌入/语音/图像/视频/审核/重排。"""
    return bool(re.search(
        r"(embed|embedding|tts|whisper|speech|audio|image|vision-only|dall|flux|sd3|"
        r"video|rerank|moderation|ocr|sdxl)", model_id, re.I))


def _headers(protocol: str, api_key: str) -> dict[str, str]:
    if protocol == "anthropic-messages":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"}
    if protocol == "google-gemini":
        return {"Content-Type": "application/json"}
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _models_url(base: str, protocol: str, api_key: str) -> str:
    base = base.rstrip("/")
    if protocol == "google-gemini":
        sep = "&" if "?" in base else "?"
        return f"{base}/models{sep}key={api_key}&pageSize=200"
    return f"{base}/models"


def parse_models(payload: dict, protocol: str) -> list[ModelInfo]:
    """把三种协议的 /models 响应归一成 ModelInfo 列表。

    端点字段名不统一（context_window / context_length / inputTokenLimit…），
    这里做宽口径读取；拿不到就按家族推断并标 auto。
    """
    raw: list[dict] = []
    if protocol == "google-gemini":
        for m in payload.get("models") or []:
            if isinstance(m, dict):
                raw.append({
                    "id": str(m.get("name") or "").split("/")[-1],
                    "display": m.get("displayName") or "",
                    "ctx": m.get("inputTokenLimit"),
                    "out": m.get("outputTokenLimit"),
                    "tools": True if m.get("supportedGenerationMethods") else None,
                })
    else:
        items = payload.get("data") or payload.get("models") or []
        for m in items if isinstance(items, list) else []:
            if not isinstance(m, dict):
                continue
            raw.append({
                "id": str(m.get("id") or m.get("name") or ""),
                "display": m.get("display_name") or m.get("displayName") or "",
                "ctx": m.get("context_window") or m.get("context_length")
                       or m.get("max_context_tokens") or m.get("max_input")
                       or m.get("inputTokenLimit"),
                # 不同端点字段名不一：UniSound 用 max_output，OpenAI 用 max_completion_tokens
                "out": m.get("max_output") or m.get("max_output_tokens")
                       or m.get("max_completion_tokens") or m.get("outputTokenLimit"),
                "tools": m.get("supports_tools") if "supports_tools" in m else None,
            })

    out: list[ModelInfo] = []
    seen: set[str] = set()
    for item in raw:
        mid = (item["id"] or "").strip()
        if not mid or mid in seen or _is_embedding_or_media(mid):
            continue
        seen.add(mid)
        ctx = _as_int(item.get("ctx"))
        max_out = _as_int(item.get("out"))
        tools = item.get("tools") if isinstance(item.get("tools"), bool) else None
        source = "reported" if (ctx or max_out) else "unknown"
        if not ctx and not max_out:
            ctx, max_out, hinted_tools = infer_capabilities(mid)
            tools = tools if tools is not None else hinted_tools
            source = "auto" if (ctx or max_out) else "unknown"
        # 输出不得超过上下文（部分端点会给出互相矛盾的数）
        if ctx and max_out and max_out > ctx:
            max_out = ctx
        out.append(ModelInfo(id=mid, display_name=str(item.get("display") or ""),
                             context_window=ctx, max_output=max_out,
                             tool_calling=tools, source=source))
    out.sort(key=lambda m: m.id)
    return out


@dataclass
class ProviderRequest:
    base_url: str
    api_key: str = ""
    protocol: str = "openai-compatible"
    model: str = ""
    max_output: int = 0
    private_ok: bool = False
    timeout_s: float = 20.0


async def _guard(req: ProviderRequest) -> str:
    """SSRF 守卫：返回规范化 base_url。内网端点必须显式 private_ok。"""
    from core import url_guard

    base = (req.base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("Base URL 不能为空")
    if not re.match(r"^https?://", base, re.I):
        raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
    if req.private_ok:
        # 只做协议与主机名解析的宽松校验；云元数据地址仍拒绝
        host = re.sub(r"^https?://", "", base).split("/")[0].split(":")[0]
        if url_guard.is_metadata_host(host):
            raise ValueError("拒绝访问云元数据地址")
        return base
    url_guard.resolve_public(
        re.sub(r"^https?://", "", base).split("/")[0].split(":")[0], 443)
    return base


async def discover(req: ProviderRequest) -> dict:
    """拉取模型列表。失败返回 ok=false + 可读原因，不抛 500。

    先校验 Key 再解析域名：Key 没填时用户需要的是「填 Key」，而不是
    「域名解析失败」这种把人带偏的网络错误。
    """
    if not req.api_key and req.protocol != "google-gemini":
        return {"ok": False, "models": [], "error": "请先填写 API Key"}
    try:
        base = await _guard(req)
    except Exception as e:
        return {"ok": False, "models": [], "error": str(e)}
    url = _models_url(base, req.protocol, req.api_key)
    try:
        async with httpx.AsyncClient(timeout=req.timeout_s, follow_redirects=False) as client:
            resp = await client.get(url, headers=_headers(req.protocol, req.api_key))
            if resp.status_code == 401:
                return {"ok": False, "models": [], "error": "鉴权失败（401）：API Key 不正确或已失效"}
            if resp.status_code == 404:
                return {"ok": False, "models": [],
                        "error": "端点没有 /models 接口（404）：可手动填写模型名，或换用支持模型列表的地址"}
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPStatusError as e:
        return {"ok": False, "models": [], "error": f"HTTP {e.response.status_code}"}
    except (httpx.HTTPError, ValueError) as e:
        return {"ok": False, "models": [], "error": f"{type(e).__name__}: {str(e)[:160]}"}
    models = parse_models(payload if isinstance(payload, dict) else {}, req.protocol)
    return {"ok": True, "models": [m.as_dict() for m in models],
            "count": len(models), "base_url": base, "protocol": req.protocol,
            "note": "" if models else "端点可达但没有返回聊天模型，可手动填写模型名"}


def _chat_payload(protocol: str, model: str, messages: list[dict],
                  max_tokens: int, temperature: float = 0.2) -> tuple[str, dict, dict]:
    """按协议构造 (url_suffix, body, extra_headers)。"""
    if protocol == "anthropic-messages":
        system = "\n".join(m["content"] for m in messages if m.get("role") == "system")
        msgs = [{"role": "assistant" if m["role"] == "assistant" else "user",
                 "content": m["content"]} for m in messages if m.get("role") != "system"]
        body = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
                "messages": msgs}
        if system:
            body["system"] = system
        return "/messages", body, {"anthropic-version": "2023-06-01"}
    if protocol == "google-gemini":
        system = "\n".join(m["content"] for m in messages if m.get("role") == "system")
        contents = [{"role": "model" if m["role"] == "assistant" else "user",
                     "parts": [{"text": m["content"]}]}
                    for m in messages if m.get("role") != "system"]
        body: dict = {"contents": contents,
                      "generationConfig": {"maxOutputTokens": max_tokens,
                                           "temperature": temperature}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return f"/models/{model}:generateContent", body, {}
    return "/chat/completions", {
        "model": model, "messages": messages, "max_tokens": max_tokens,
        "temperature": temperature,
    }, {}


def extract_text(protocol: str, payload: dict) -> str:
    """从三种协议的成功响应里取文本。"""
    if protocol == "anthropic-messages":
        parts = payload.get("content") or []
        return "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()
    if protocol == "google-gemini":
        cands = payload.get("candidates") or []
        if not cands:
            return ""
        parts = (cands[0].get("content") or {}).get("parts") or []
        return "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()
    choices = payload.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    # 推理模型（u2 / deepseek-r1 等）先输出 reasoning_content；max_tokens 很小时
    # content 可能为空。连通性判定用任一非空文本即可，不把「推理中」当失败。
    text = msg.get("content") or msg.get("reasoning_content") or ""
    return str(text).strip()


async def test_connection(req: ProviderRequest) -> dict:
    """真发一次最小请求，验证地址/Key/模型三者都能用。"""
    if not req.model:
        return {"ok": False, "reply": "请先选择或填写模型名", "model": "", "latency_ms": 0}
    if not req.api_key and req.protocol != "google-gemini":
        return {"ok": False, "reply": "请先填写 API Key", "model": req.model, "latency_ms": 0}
    try:
        base = await _guard(req)
    except Exception as e:
        return {"ok": False, "reply": str(e), "model": req.model, "latency_ms": 0}
    import time as _t

    suffix, body, extra = _chat_payload(
        req.protocol, req.model,
        [{"role": "user", "content": "回复「正常」两个字即可"}],
        max_tokens=256)
    url = f"{base}{suffix}"
    if req.protocol == "google-gemini":
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}key={req.api_key}"
    headers = {**_headers(req.protocol, req.api_key), **extra}
    started = _t.monotonic()
    try:
        async with httpx.AsyncClient(timeout=req.timeout_s, follow_redirects=False) as client:
            resp = await client.post(url, headers=headers, json=body)
            latency = int((_t.monotonic() - started) * 1000)
            if resp.status_code == 401:
                return {"ok": False, "reply": "鉴权失败（401）：API Key 不正确", "model": req.model,
                        "latency_ms": latency}
            if resp.status_code == 404:
                return {"ok": False, "reply": f"模型或路径不存在（404）：{req.model}", "model": req.model,
                        "latency_ms": latency}
            resp.raise_for_status()
            text = extract_text(req.protocol, resp.json())
    except httpx.TimeoutException:
        return {"ok": False, "reply": "请求超时", "model": req.model,
                "latency_ms": int((_t.monotonic() - started) * 1000)}
    except (httpx.HTTPError, ValueError) as e:
        return {"ok": False, "reply": f"{type(e).__name__}: {str(e)[:160]}", "model": req.model,
                "latency_ms": int((_t.monotonic() - started) * 1000)}
    return {"ok": True, "reply": text[:60] or "（空回复但请求成功）", "model": req.model,
            "latency_ms": latency}
