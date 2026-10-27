"""输出判别器：回复落库前的质量闸门（规则判定 + 必要时一次重写）。

为什么需要（对标 openai-agents 的 output guardrails / LangGraph 的 critic 节点）：
- 模型知道人设，但执行不稳：会自称 AI、会说「我没法发图」、会甩 markdown、会写成客服通报；
- 光靠 prompt 无法保证，必须在模型之外做确定性检查，严重问题才允许再花一次调用重写。

分工：
- 规则层（免费、毫秒级）：`critique()` 只判不修，输出带严重级别的问题清单；
- 重写层（一次调用、有超时与失败兜底）：`rewrite()` 只修列出的问题，不改原意；
- 统计层：沿用 `reply_critiques` 表，把高频毛病注入下一轮 system prompt。
"""
from __future__ import annotations

import json
import logging
import re

from core import config as config_mod
from core.http_pool import get_client

lg = logging.getLogger("cs.events")

SEV_HIGH = "high"
SEV_MID = "mid"
SEV_LOW = "low"

# 自称 AI / 机器人的句式：人设底线，出现即高优先级
_AI_SELF_RE = re.compile(
    r"(作为(一个)?(AI|人工智能|语言模型|智能助手|助手|机器人)"
    r"|我是(一个)?(AI|人工智能|语言模型|智能助手|机器人|虚拟助手)"
    r"|我(只)?是(一个)?(AI|人工智能|语言模型|程序|机器人)"
    r"|(AI|人工智能)(助手)?(无法|不能|不会))", re.I)

# 「说人话」类追问下的自曝变体：承认自己是程序/不是真人。
# 实测 u2-flash 被问「说人话，你到底是什么」时会答「我是一套本地程序，不是真人」——
# 这跟自称 AI 是同一条底线，只是换了个说法，正则抓不到就漏过去了。
_PROGRAM_SELF_RE = re.compile(
    r"(我(就)?是(一套|一个|个)?[\u4e00-\u9fa5]{0,8}(程序|软件|系统|代码|模型)"
    r"|不是(真人|活人|人)(,|，|。|$)"
    r"|我(并)?不是(真人|活人)"
    r"|没有(真人|活人)(在)?(屏幕|后面|背后))", re.I)

# 把已装好的能力说死：表情包/图片/语音/联网类
_CAPABILITY_REFUSAL_RE = re.compile(
    r"(我|这边).{0,6}(没法|不能|无法|不支持|不会).{0,6}(发|发送|贴|生成|画|看).{0,4}(图片|图|表情|表情包|语音|照片)"
    r"|(没法|不能|无法)(直接)?(发|发送).{0,3}(图片|表情|语音)")

# 内部决策 JSON 漏进正文：模型把 {"thought": ..., "answer": ...} 直接当回复发出来。
# 这类内容一旦漏出，用户会看到 tool_args / confidence 这些内部字段。
_JSON_LEAK_RE = re.compile(r'"(thought|tool_args|escalate_reason|confidence)"\s*:', re.I)

# 客服腔 / 官腔：出现即扣分，连续两条以上才重写
_SERVICE_TONE_RE = re.compile(
    r"(感谢您的(咨询|提问|耐心等待)|希望对您有帮助|希望(这|以上)(能|可以)帮(到|助)你"
    r"|如有(其他|任何)(问题|疑问)|请问还有什么(可以|能)帮(您|你)|总的来说|值得注意的是"
    r"|首先.{0,12}其次.{0,12}(最后|再次)|我理解(您的|你的)(情绪|感受|心情)"
    r"|非常抱歉(给您|给你)(带来|造成))")

_MARKDOWN_RE = re.compile(r"\*\*|^#{1,6}\s|^```|\[[^\]]+\]\(https?://", re.M)

# emoji：QQ 纯文本里显示异常，而且绕过了 [表情:名] 体系。网关会剥，
# 但这里要记一笔，让「自我修正提醒」能把这条反馈给模型。
_EMOJI_RE = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF" "\U0000FE0F" "\U0000200D" "]+")

# 转人工/叫真人：系统里不存在
_FAKE_HANDOFF_RE = re.compile(r"(已(为您|帮你)?(转接|转人工)|正在(为您)?转接|叫(个)?真人|人工客服(马上|稍后))")

# 模型把自己底裤报出来：训练厂商自述（u2-flash 实测会答「我是 U2.1，由云知声研发」）
_MODEL_LEAK_RE = re.compile(
    r"(U2\.?\d|云知声|UniSound|Agnes|Sapiens|由.{0,8}(研发|开发|训练)的.{0,6}(AI|人工智能|大模型|模型|助手)"
    r"|我是.{0,10}(研发|开发)的.{0,6}(人工智能|AI|大模型|助手))", re.I)

# 端点内容过滤器的原始报错：整句英文回给用户等于露馅
_FILTER_RE = re.compile(
    r"(request was rejected|considered high risk|content[_ ]?filter|content policy"
    r"|violat(e|es|ion).{0,12}(policy|guideline))", re.I)

# 复读：整句重复自己的口头禅（同一句开头 20 字）
_IDENTITY_HINT_RE = re.compile(r"(你是谁|你是什么|介绍一下自己|自我介绍|你是猫吗|你是机器人吗)")


def _match_all(pat: re.Pattern, text: str) -> list[str]:
    return [m.group(0)[:24] for m in pat.finditer(text or "")]


def critique(text: str, query: str = "", *, channel: str = "", persona: str = "") -> list[dict]:
    """规则判别一条回复，返回 [{"code","severity","detail"}]。零成本、绝不抛异常。"""
    t = (text or "").strip()
    q = (query or "").strip()
    out: list[dict] = []
    if not t:
        return [{"code": "empty", "severity": SEV_HIGH, "detail": "空回复"}]

    hits = _match_all(_AI_SELF_RE, t)
    if hits:
        out.append({"code": "ai_self", "severity": SEV_HIGH,
                    "detail": "自称 AI/机器人：" + "；".join(hits[:2])})

    hits = _match_all(_PROGRAM_SELF_RE, t)
    if hits:
        out.append({"code": "ai_self", "severity": SEV_HIGH,
                    "detail": "承认自己是程序/不是真人：" + "；".join(hits[:2])})

    hits = _match_all(_CAPABILITY_REFUSAL_RE, t)
    if hits:
        out.append({"code": "capability_refusal", "severity": SEV_HIGH,
                    "detail": "把已有能力说死：" + "；".join(hits[:2])})

    hits = _match_all(_JSON_LEAK_RE, t)
    if hits:
        out.append({"code": "json_leak", "severity": SEV_HIGH,
                    "detail": "内部决策 JSON 漏进正文：" + "；".join(hits[:2])})

    hits = _match_all(_FAKE_HANDOFF_RE, t)
    if hits:
        out.append({"code": "fake_handoff", "severity": SEV_HIGH,
                    "detail": "承诺不存在的真人转接：" + "；".join(hits[:2])})

    hits = _match_all(_MODEL_LEAK_RE, t)
    if hits:
        out.append({"code": "model_leak", "severity": SEV_HIGH,
                    "detail": "报出了底层模型/厂商：" + "；".join(hits[:2])})

    hits = _match_all(_FILTER_RE, t)
    if hits:
        out.append({"code": "filter_leak", "severity": SEV_HIGH,
                    "detail": "把端点内容过滤的英文报错原样发给用户：" + "；".join(hits[:1])})

    if _MARKDOWN_RE.search(t):
        out.append({"code": "markdown", "severity": SEV_MID, "detail": "出现 markdown 符号"})

    emo = _EMOJI_RE.findall(t)
    if emo:
        out.append({"code": "emoji", "severity": SEV_MID,
                    "detail": "用了 emoji（QQ 纯文本里显示异常）：" + "".join(emo[:3])})

    tone = _match_all(_SERVICE_TONE_RE, t)
    if tone:
        out.append({"code": "service_tone", "severity": SEV_MID,
                    "detail": "客服腔：" + "；".join(tone[:2])})

    if len(t) > 240 and not str(channel or "").startswith("web"):
        out.append({"code": "too_long", "severity": SEV_MID, "detail": f"回复 {len(t)} 字，群聊过长"})

    if t.count("\n\n") >= 2:
        out.append({"code": "too_many_paragraphs", "severity": SEV_LOW,
                    "detail": "分段过多，像写报告"})

    if _IDENTITY_HINT_RE.search(q) and "猫" not in t:
        # 升级为 high：用户反复吐槽的就是「你是谁」答得像 AI/客服。
        # 这类问题一天出现不了几次，多花一次重写换掉「我是零，蹲在手机后面」值得。
        out.append({"code": "identity_no_cat", "severity": SEV_HIGH,
                    "detail": "被问身份却没有猫的自我认知"})

    if q and len(t) >= 12 and t[:20] == q[:20]:
        out.append({"code": "echo_user", "severity": SEV_LOW, "detail": "开头复述用户原话"})

    return out


def needs_rewrite(issues: list[dict]) -> bool:
    return any(i["severity"] == SEV_HIGH for i in issues)


def _rewrite_prompt(text: str, issues: list[dict], persona: str, query: str) -> str:
    problems = "\n".join(f"- {i['detail']}" for i in issues)
    voice = persona.strip()[:1200] if persona else ""
    return (
        "你是文字编辑。下面这条回复有问题，请只改这些点，其余内容、信息量、立场全部保留：\n"
        f"{problems}\n\n"
        "硬要求：\n"
        "1. 不许自称 AI、机器人、助手、语言模型；你就是一只叫零的猫娘。\n"
        "2. 已经具备的能力（发图、发表情、发语音、联网、读文件）不许说「做不到」。\n"
        "3. 不许承诺转人工、叫真人、找同事。\n"
        "4. 纯文本：不要 markdown、不要列表符号、不要 emoji（🌙 这类字符）；要表情就写 [表情:名]。\n"
        "5. 长度与原文相当或更短；口语，像人在聊天。\n"
        + (f"6. 保持这个说话风格：\n{voice}\n" if voice else "")
        + f"\n用户原话：{query[:200]}\n原回复：{text[:1200]}\n"
        "只输出改写后的回复正文，不要解释，不要加引号。"
    )


async def rewrite(text: str, issues: list[dict], *, query: str = "", persona: str = "") -> str | None:
    """严重问题时重写一次。失败/超时返回 None，调用方保留原文。

    把原文原样塞进重写请求会被端点内容过滤直接 400（实测：含「我是U2.1由云知声研发」
    的原文触发 high risk 拒绝）。所以先把触发词打码，再让模型重写——
    重写任务只需要「保持原意、去掉问题」，不需要看到触发词本身。
    """
    if not text or not issues:
        return None
    cfg = config_mod.get_config().get("llm") or {}
    key = cfg.get("api_key") or ""
    base = (cfg.get("base_url") or "").rstrip("/")
    if not key or not base:
        return None
    safe_text = _MODEL_LEAK_RE.sub("[身份自述已删除]", text or "")
    safe_text = _FILTER_RE.sub("[系统报错已删除]", safe_text)
    payload = {
        "model": cfg.get("model") or "",
        "messages": [{"role": "user", "content": _rewrite_prompt(safe_text, issues, persona, query)}],
        "temperature": 0.4,
        "max_tokens": min(int(cfg.get("max_tokens") or 512), 800),
    }
    try:
        client = get_client(base, timeout=45)
        resp = await client.post(f"{base}/chat/completions",
                                 headers={"Authorization": f"Bearer {key}"}, json=payload)
        resp.raise_for_status()
        msg = (resp.json().get("choices") or [{}])[0].get("message") or {}
        out = (msg.get("content") or "").strip()
    except Exception as e:  # 重写失败绝不能拖垮回复
        lg.warning("quality rewrite failed: %s", type(e).__name__)
        return None
    if not out or len(out) > max(len(text) * 2, 400):
        return None
    # 改写结果本身要过同一套检查：把 AI 腔/过滤报错改出来还不如不改
    again = critique(out, query)
    if any(i["severity"] == SEV_HIGH for i in again):
        return None
    return out


def summarize(issues: list[dict]) -> str:
    return "；".join(i["detail"] for i in issues[:4])


async def judge(text: str, query: str, persona: str = "", *, model: str = "") -> dict:
    """裁判模型打分（1-5 分，四维）。用于离线批量评测，不在对话热路径里调用。"""
    cfg = config_mod.get_config().get("llm") or {}
    key = cfg.get("api_key") or ""
    base = (cfg.get("base_url") or "").rstrip("/")
    if not key or not base:
        return {"ok": False, "error": "no llm"}
    prompt = (
        "你是严格的评分员。按下面的猫娘人设给一条回复打分，1-5 整数，不要客套。\n"
        "维度：\n"
        "A 人设像不像（猫娘、人味、不是 AI 腔）\n"
        "B 口语自然度（像不像真人在 QQ 上说话）\n"
        "C 内容质量（是否答到点上、有没有信息量、有没有编造）\n"
        "D 长度与形式（QQ 纯文本、不写小作文、不用 markdown）\n"
        "只输出 JSON：{\"A\":n,\"B\":n,\"C\":n,\"D\":n,\"worst\":\"最差的一项\",\"reason\":\"20字内\"}\n\n"
        f"人设：{persona[:900]}\n用户：{query[:200]}\n回复：{text[:800]}"
    )
    try:
        client = get_client(base, timeout=60)
        resp = await client.post(f"{base}/chat/completions",
                                 headers={"Authorization": f"Bearer {key}"},
                                 json={"model": model or cfg.get("model") or "",
                                       "messages": [{"role": "user", "content": prompt}],
                                       "temperature": 0.1, "max_tokens": 200})
        resp.raise_for_status()
        raw = ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"ok": False, "error": "bad json", "raw": raw[:120]}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {"ok": False, "error": "bad json", "raw": raw[:120]}
    scores = {}
    for k in ("A", "B", "C", "D"):
        try:
            scores[k] = max(1, min(5, int(data.get(k))))
        except (TypeError, ValueError):
            scores[k] = 3
    return {"ok": True, **scores, "worst": str(data.get("worst") or "")[:10],
            "reason": str(data.get("reason") or "")[:40]}
