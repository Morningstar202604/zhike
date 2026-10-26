"""技能（Skills）：把「一组能力 + 使用指南」注入系统提示词。

与工具的区别：
- 工具（tools）是模型可以**调用**的函数；
- 技能（skills）是告诉模型**什么时候该用哪个工具、按什么流程做**的说明书。
  模型不会因为注册了 read_file 就知道「先读目录再读文件」，技能补的正是这一层。

配置存 config.skills.items（数组），每项：
  {id, name, description, guide, enabled, builtin}

内置技能永远存在（可停用、不可删除），自定义技能可增删。
"""
from __future__ import annotations

import re
import uuid

BUILTIN_SKILLS: list[dict] = [
    {
        "id": "builtin-web-research",
        "name": "联网查证",
        "description": "需要实时信息、新闻、价格、版本号时先搜再答",
        "guide": (
            "遇到时效性问题（新闻/价格/版本/政策变动）先用 web_search，"
            "需要细节再对最相关的一两条用 read_webpage。只引用你真正读到过的内容，"
            "搜不到就直说没查到，不要用记忆里的旧数据冒充最新。"
        ),
        "builtin": True,
    },
    {
        "id": "builtin-file-ops",
        "name": "文件操作",
        "description": "读写本机文件时先看目录再动手",
        "guide": (
            "涉及本机文件时：先用 list_files 看清目录结构，再用 read_file 读具体文件；"
            "写文件前若不确定目标是否存在，先 read_file 确认。路径必须落在执行器允许的根目录内，"
            "被策略拒绝就如实说明，不要换个路径硬试。"
        ),
        "builtin": True,
    },
    {
        "id": "builtin-reminder",
        "name": "日程提醒",
        "description": "把「几点做什么」落成一条真实提醒",
        "guide": (
            "用户提到要在某个时间做某事时，调用 remind 建提醒，"
            "把相对时间（10 分钟后 / 明天 9 点）原样传给 when，由系统统一解析。"
            "建完用一句话确认具体时间，不要只回「好的」。"
        ),
        "builtin": True,
    },
    {
        "id": "builtin-memory",
        "name": "长期记忆",
        "description": "值得跨会话记住的偏好与事实才写入",
        "guide": (
            "只在用户表达了稳定偏好或长期事实（口味、住址、称呼、工作习惯）时调用 remember；"
            "一次性的临时信息不要写。回答前可用 recall_memory 查一下有没有相关记录，"
            "有就自然带出，别复述「根据我的记忆」这种机制腔。"
        ),
        "builtin": True,
    },
    {
        "id": "builtin-doc",
        "name": "文档解析",
        "description": "PDF/Word/Excel 先解析再回答",
        "guide": (
            "用户给了 PDF/DOCX/XLSX 路径或链接时，用 parse_document 取正文再回答；"
            "解析失败要说清是格式不支持还是文件加密，别假装读过。"
        ),
        "builtin": True,
    },
]


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return base or f"skill-{uuid.uuid4().hex[:6]}"


def normalize_items(items) -> list[dict]:
    """校验并补全技能定义：内置项缺失就补回，字段类型错误按默认值修。"""
    out: list[dict] = []
    seen: set[str] = set()
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        sid = str(raw.get("id") or "").strip() or _slug(name)
        if sid in seen:
            continue
        seen.add(sid)
        out.append({
            "id": sid,
            "name": name[:40],
            "description": str(raw.get("description") or "")[:200],
            "guide": str(raw.get("guide") or "")[:2000],
            "enabled": False if raw.get("enabled") is False or raw.get("enabled") == 0 else True,
            "builtin": bool(raw.get("builtin")),
        })
    # 内置技能必须存在（用户可以停用，但删不掉）：缺哪个补哪个
    for b in BUILTIN_SKILLS:
        if not any(x["id"] == b["id"] for x in out):
            out.append(dict(b, enabled=True))
    return out


def render(cfg: dict) -> str:
    """把启用的技能渲染成提示词片段；没有启用项返回空串。"""
    items = normalize_items((cfg.get("skills") or {}).get("items"))
    active = [s for s in items if s["enabled"] and (s["guide"] or s["description"])]
    if not active:
        return ""
    lines = ["【可用技能】按场景选用，不要为了用技能而用："]
    for s in active:
        body = s["guide"] or s["description"]
        lines.append(f"- {s['name']}：{body}")
    return "\n".join(lines)


def names(cfg: dict) -> list[str]:
    return [s["name"] for s in normalize_items((cfg.get("skills") or {}).get("items"))
            if s["enabled"]]
