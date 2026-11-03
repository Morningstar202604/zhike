"""角色设定一致性校验：防止角色"说着说着就不是他了"。

## 这份文件解决什么问题

角色漂移不是玄学，它有具体形态，每一种都能被机器检出：

1. **自称漂移**：零自称「我」还算正常，但她不该自称「本助手」「AI 助手」。
   每个角色都有自己的身份锚点（名字、物种、职业），一旦出现别的身份词就是漂移。
2. **口癖丢失**：零的「喵」、老陈的「先看日志」，是人物的指纹。如果连续多轮
   一次都不出现，说明模型回到了通用助手腔。
3. **设定冲突**：资料写「有个铲屎的，不提名字」，回复里却编出主人名字；
   资料写「不说具体年龄」，回复里却说「我今年十八」。这类是硬错误。
4. **语气塌陷**：本该毒舌的角色开始说「希望这能帮到你」。这是最伤沉浸感的。

## 为什么不用"再嘱咐模型一次"

提示词里写「保持人设」几乎无效——模型没有"我刚才像不像自己"的自我监控。
有效的是把判据写成**可执行规则**，在生成后立刻检查，不合格就重写一次。
本模块只做检查（纯函数、可单测），重试逻辑在 character_guard 调用方。

## 判据从哪来

不写死在代码里，从角色卡自己声明：
- ``taboos`` 里的词 → 命中即违规
- ``voice_samples`` 里的字词 → 用来推断"这个角色该有什么口癖"
- ``fields`` 里的身份锚点 → 推断"不该出现的身份词"

这样新增角色只要写好资料卡，校验自动生效，不用改代码。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.character import CharacterCard

# 所有角色都不该出现的"通用助手腔"。这些词一出，不管什么角色都算塌陷。
GENERIC_ASSISTANT_PHRASES = (
    "作为AI", "作为 AI", "作为一个AI", "作为人工智能",
    "希望这能帮到你", "希望对你有帮助", "希望以上",
    "有什么可以帮您的", "很高兴为您服务", "请问还有什么",
    "总的来说", "综上所述", "值得注意的是",
)

# 身份词：出现即说明角色开始自称别的东西
IDENTITY_DRIFT_PHRASES = (
    "本助手", "我是AI", "我是 AI", "我是一个AI", "我是语言模型",
    "我只是个程序", "我作为助手", "我的算法",
)


@dataclass
class Violation:
    """一条违规。kind 用于统计，detail 用于给模型的原话反馈。"""
    kind: str
    detail: str
    excerpt: str = ""


@dataclass
class CheckResult:
    ok: bool
    violations: list[Violation] = field(default_factory=list)

    @property
    def kinds(self) -> set[str]:
        return {v.kind for v in self.violations}

    def feedback(self) -> str:
        """生成给模型看的一句话修正要求（用于重写）。"""
        if self.ok:
            return ""
        parts = []
        for v in self.violations:
            parts.append(f"- {v.detail}")
        return ("上面这段回答不像这个角色会说的话，问题在这儿：\n"
                + "\n".join(parts)
                + "\n请用这个角色的口吻重说一遍，内容可以相同，语气要是他本人的。")


def _norm(text: str) -> str:
    return (text or "").strip()


def check_generic_assistant(text: str) -> list[Violation]:
    """检出通用助手腔。任何角色出现这些词都算人设塌陷。"""
    t = _norm(text)
    out = []
    for phrase in GENERIC_ASSISTANT_PHRASES:
        if phrase in t:
            out.append(Violation(
                kind="assistant_tone",
                detail=f"不要用「{phrase}」这种客服话术",
                excerpt=phrase,
            ))
    return out


def check_identity(text: str, card: CharacterCard) -> list[Violation]:
    """检出身份漂移：角色自称成别的东西。"""
    t = _norm(text)
    out = []
    for phrase in IDENTITY_DRIFT_PHRASES:
        if phrase in t:
            out.append(Violation(
                kind="identity_drift",
                detail=f"你是「{card.name}」，不要说「{phrase}」",
                excerpt=phrase,
            ))
    return out


def check_taboos(text: str, card: CharacterCard) -> list[Violation]:
    """命中角色卡里明确禁止的说法。

    taboos 是自然语言写的（"不编造事实"），没法直接做子串匹配。
    这里只处理能机械判定的那部分：引号里明确给出的禁用词。
    形如 「不要输出 markdown 粗体」 这种规则交给上线判据，不做文本匹配。
    """
    t = _norm(text)
    out = []
    for taboo in card.taboos:
        # 抽出 taboo 里用「」或 "" 包起来的词，视作硬禁用
        for quoted in re.findall(r"[「\"]([^」\"]{2,20})[」\"]", taboo):
            if quoted in t and quoted not in ("", card.name):
                out.append(Violation(
                    kind="taboo",
                    detail=f"不能用「{quoted}」（角色设定里明确禁止）",
                    excerpt=quoted,
                ))
    return out


def _signature_fragments(card: CharacterCard) -> set[str]:
    """角色卡显式声明的标志性口癖。

    早期版本尝试"从语料自动推断"（统计多条语料里重复出现的两字片段），
    实测完全不可用：
    - 零的标志是单字「喵」，两字切分抓不到；
    - 推断出来的反而多是「你这」这类噪音，把正常回复误判成漂移。
    所以改为由角色卡显式声明 ``catchphrases``，判据可信、可解释。
    """
    return {c for c in card.catchphrases if c}


def check_voice_signature(text: str, card: CharacterCard) -> list[Violation]:
    """口癖检查：回复里是否还带着角色的标志性用词。

    只在角色卡显式声明了 catchphrases 时生效（多数角色不需要固定口癖）。
    短回复（<25 字）不检查——一句「嗯」也可能完全符合人设，
    宁可漏判，不要把正常回复误判成漂移。
    """
    t = _norm(text)
    if len(t) < 25:
        return []
    signature = _signature_fragments(card)
    if not signature:
        return []
    if any(sig in t for sig in signature):
        return []
    return [Violation(
        kind="voice_lost",
        detail=f"这段少了这个角色的标志性说法（{'/'.join(sorted(signature))}）",
        excerpt="",
    )]


def check_reply(text: str, card: CharacterCard, *, strict_voice: bool = False) -> CheckResult:
    """完整校验。strict_voice=True 时把口癖丢失也算失败。"""
    vs: list[Violation] = []
    vs += check_generic_assistant(text)
    vs += check_identity(text, card)
    vs += check_taboos(text, card)
    if strict_voice:
        vs += check_voice_signature(text, card)
    # 同一条违规可能被多个判据抓到，按 (kind, excerpt) 去重
    seen = set()
    deduped = []
    for v in vs:
        key = (v.kind, v.excerpt)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(v)
    return CheckResult(ok=not deduped, violations=deduped)
