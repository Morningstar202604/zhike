"""角色卡：把「设定」从一段提示词拆成可长期维护的资料。

## 为什么要有这一层

之前每个角色就是提示词里的一段文字。后果有三个，都是实际踩到的：

1. **设定会漂**。模型每轮重新读一遍设定，没有"权威出处"约束它，聊到后面
   就开始自创：住址、年龄、经历随口编，而且前后矛盾。角色不像"活人"，
   像每次都被临时捏出来的。
2. **改一处要重写全文**。想调角色的某个癖好，得在几千字里找到那句话改；
   改完又担心破坏了别的段落。
3. **资料和表现混在一起**。角色的历史背景、口头禅、忌讳、喜欢什么，
   本该是"资料"，却被写成了"你要这样说话"的指令，模型只能照做，不会
   真的"知道"。

所以本模块把角色拆成两层：

- **设定资料（canon）**：结构化、可检索、可校验。同一个事实只有一个出处。
  存在 ``core/data/characters/<id>.json``，是角色卡的可维护形态。
- **渲染（render）**：把资料按固定顺序拼成系统提示词。拼装顺序固定，
  保证每次给模型的"世界观"一致。

## 防漂移不是靠嘱咐模型「不要编」

提示词里写「不要编造」是没用的——模型没有"我编了吗"的自我监控。
真正有效的是两条：

1. **把可回答的事实提前给全**。模型编造，多半是因为问到了资料里没有的东西。
   资料越完整，编的空间越小。
2. **明确划出"资料没有"的边界**。告诉它「这些事资料里没有，被问到就说不知道
   或打太极」，比笼统的「别编」有效得多。

``unknown_policy`` 字段就是干这个的：每个角色显式声明"资料没覆盖时怎么办"。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CHARACTER_DIR = Path(__file__).resolve().parent / "data" / "characters"

# 渲染顺序固定：先"你是谁"，再"你的世界"，最后"你怎么说话"。
# 顺序有讲究——越靠前的越不容易被忽略。事实放前面，风格放后面。
_RENDER_ORDER = (
    ("identity", "【身份】"),
    ("background", "【来历】"),
    ("world", "【所处的世界】"),
    ("appearance", "【外形】"),
    ("personality", "【性格】"),
    ("habits", "【习惯与癖好】"),
    ("relationships", "【关系】"),
    ("speech", "【说话方式】"),
    ("quirks", "【小动作与口癖】"),
    ("boundaries", "【分寸与红线】"),
)


@dataclass(frozen=True)
class CharacterCard:
    """一个角色的完整设定资料。

    ``fields`` 里放结构化事实（dict），``narrative`` 放成段的描述（str）。
    两者分开：事实要能被校验和引用，叙述只负责气氛。
    """

    id: str
    name: str
    tagline: str = ""
    # 结构化事实：key 是字段名，value 是内容。被问到时模型应当照此回答。
    fields: dict = field(default_factory=dict)
    # 成段叙述：来历、世界、性格等。按 _RENDER_ORDER 拼装。
    narrative: dict = field(default_factory=dict)
    # 资料没覆盖时怎么办。不能只写"不要编"——要给可执行的话术。
    unknown_policy: str = ""
    # 角色卡的"禁区"：明确不许做的事，比笼统的"注意分寸"有用
    taboos: tuple[str, ...] = ()
    # 参考语料：这个角色会怎么说。给模型定调，比形容词有效
    voice_samples: tuple[str, ...] = ()
    # 标志性口癖（显式声明）。刻意不"从语料自动推断"——实测推断出的
    # 两字片段要么是「你这」这种噪音，要么完全抓不到单字口癖（零的「喵」），
    # 靠猜必然误判。角色卡自己写清楚最可靠。
    catchphrases: tuple[str, ...] = ()
    # 每 N 条回复至少出现一次口癖；0 表示不强制（多数角色不需要）
    catchphrase_period: int = 0
    temperature: float = 0.8
    version: int = 1

    def fact(self, key: str, default: str = "") -> str:
        v = self.fields.get(key, default)
        return str(v) if v is not None else default

    def render(self) -> str:
        """渲染成系统提示词片段。

        渲染规则：
        - 空字段整段跳过（不输出空标题，那会让模型以为"这里应该有内容"）
        - 结构化事实优先用「- 键：值」列表，模型对列表的遵循度高于散文
        - 语料单独一节，明确标注是"参考语气"而不是"照抄"
        """
        parts: list[str] = []

        head = f"你是「{self.name}」。"
        if self.tagline:
            head += self.tagline
        parts.append(head)

        if self.fields:
            lines = [f"- {k}：{v}" for k, v in self.fields.items() if str(v).strip()]
            if lines:
                parts.append("【设定事实】\n以下是你确定知道的事，被问到就照此回答，不要改口：\n"
                             + "\n".join(lines))

        for key, title in _RENDER_ORDER:
            body = str(self.narrative.get(key) or "").strip()
            if body:
                parts.append(f"{title}\n{body}")

        if self.voice_samples:
            samples = "\n".join(f"· {s}" for s in self.voice_samples)
            parts.append("【参考语气】\n下面是你平时说话的样子，学的是语气和节奏，不是照抄句子：\n" + samples)

        if self.taboos:
            parts.append("【不许做的事】\n" + "\n".join(f"- {t}" for t in self.taboos))

        if self.unknown_policy:
            parts.append("【资料没有的事】\n" + self.unknown_policy)

        return "\n\n".join(parts)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "tagline": self.tagline,
            "fields": dict(self.fields),
            "narrative": dict(self.narrative),
            "unknown_policy": self.unknown_policy,
            "taboos": list(self.taboos),
            "voice_samples": list(self.voice_samples),
            "catchphrases": list(self.catchphrases),
            "catchphrase_period": self.catchphrase_period,
            "temperature": self.temperature,
            "version": self.version,
        }


def load_card(path: Path) -> CharacterCard:
    """从 JSON 读一张角色卡。字段缺失时用默认值，不因单个角色写坏而崩。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    return CharacterCard(
        id=str(data.get("id") or path.stem),
        name=str(data.get("name") or path.stem),
        tagline=str(data.get("tagline") or ""),
        fields=dict(data.get("fields") or {}),
        narrative=dict(data.get("narrative") or {}),
        unknown_policy=str(data.get("unknown_policy") or ""),
        taboos=tuple(data.get("taboos") or ()),
        voice_samples=tuple(data.get("voice_samples") or ()),
        catchphrases=tuple(data.get("catchphrases") or ()),
        catchphrase_period=int(data.get("catchphrase_period") or 0),
        temperature=float(data.get("temperature") or 0.8),
        version=int(data.get("version") or 1),
    )


def load_all(directory: Path | None = None) -> dict[str, CharacterCard]:
    """读全部角色卡。坏文件跳过，不影响其它角色可用。

    只认真正像角色卡的文件：必须能解析成 JSON 对象，且带有 id 或 name 字段。
    为什么不能只看 ``*.json``：同一个目录里可能混进别的配置文件
    （实测测试夹具就把 kb.json 写进了同一 tmp_path），盲glob 会把它们当角色
    卡加载，产生幽灵角色。
    """
    root = directory or CHARACTER_DIR
    out: dict[str, CharacterCard] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(raw, dict) or not ("id" in raw or "name" in raw):
            continue
        try:
            card = load_card(path)
        except (OSError, ValueError, TypeError):
            continue
        out[card.id] = card
    return out


def render_for(preset_id: str, directory: Path | None = None) -> str:
    """取某角色的渲染结果；没有资料卡时返回空串，调用方回退到原提示词。"""
    card = load_all(directory).get(preset_id)
    return card.render() if card else ""


def card_for_persona(persona_text: str, directory: Path | None = None) -> CharacterCard | None:
    """反查：这段提示词是哪张角色卡渲染出来的？查不到返回 None。

    为什么用「渲染结果逐字比对」而不是记一个 preset id 到配置里：
    - 提示词是用户可编辑的单一事实源，apply 之后用户可能微调，
      微调过就不再等于任何卡片的渲染稿——此时宁可跳过校验，
      也不能拿一张对不上的卡去判用户的自定义角色。
    - 逐字比对零状态、可重现，配置里不用多一个要同步的键。

    卡片只有 15 张且渲染结果可缓存，比对成本可忽略（应用层无需操心）。
    """
    if not persona_text:
        return None
    for card in load_all(directory).values():
        if card.render() == persona_text:
            return card
    return None
