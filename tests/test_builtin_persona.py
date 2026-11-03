"""内置角色回归：默认人设必须与前端预设逐字节一致，且猫味条款不丢。"""
import pathlib
import re

from core import config, persona


def test_default_persona_is_used_by_config(monkeypatch):
    monkeypatch.delenv("USER_PERSONA", raising=False)
    assert config.get_config()["context"]["persona"] == persona.DEFAULT_PERSONA


def test_env_persona_overrides_builtin(monkeypatch):
    monkeypatch.setenv("USER_PERSONA", "  测试人设  ")
    assert persona.default_persona() == "测试人设"


def test_builtin_persona_keeps_cat_traits():
    text = persona.DEFAULT_PERSONA
    assert "喵～" in text and "本喵" in text and "猫娘" in text
    assert "不是程序" in text
    assert len(text) > 2000


def test_frontend_no_longer_duplicates_persona_text():
    """预设文案必须是后端单一事实源：前端不得再内嵌长人设副本。

    历史实现是「后端一份 + TSX 再抄一份」，靠逐字节比对测试维持同步；
    加角色要改两处、漏一处就失败。现在前端从 /api/personas 拉取，
    这里守的是「不许回退」：TSX 里出现超长模板字符串即判失败。
    """
    tsx = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "src" / "BotPersona.tsx"
    source = tsx.read_text(encoding="utf-8")
    assert "个人助理" not in source and "猫娘——萌萌的小猫" not in source,         "BotPersona.tsx 又内嵌了人设正文，请改为从 /api/personas 读取"
    longest = max((len(m) for m in re.findall(r"`([^`]*)`", source, re.S)), default=0)
    assert longest < 400, f"TSX 里出现 {longest} 字的模板字符串，疑似重新内嵌人设"


def test_presets_registry_is_consistent():
    from core import personas

    ids = [p.id for p in personas.PRESETS]
    assert len(ids) == len(set(ids)), f"预设 id 重复：{ids}"
    names = [p.name for p in personas.PRESETS]
    assert len(names) == len(set(names)), f"预设名重复：{names}"
    assert len(personas.PRESETS) >= 12, "预设角色太少，至少覆盖 6 个分类各 2 个"
    assert set(p.category for p in personas.PRESETS) == set(personas.CATEGORIES)
    for p in personas.PRESETS:
        assert p.persona.strip(), f"{p.id} 人设为空"
        assert len(p.persona) >= 120, f"{p.id} 人设过短，无法约束行为"
        assert 0 <= p.temperature <= 1.5, f"{p.id} 温度非法"
        assert p.description.strip(), f"{p.id} 缺少描述"
    # 默认角色必须是猫娘「零」，与配置文件默认值一致
    assert personas.default().persona == persona.DEFAULT_PERSONA


def test_personas_api_serves_all_presets(client):
    r = client.get("/api/personas")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["default_id"] == "zero"
    ids = [p["id"] for p in data["presets"]]
    assert "zero" in ids and "tech" in ids and "analyst" in ids
    assert all(p.get("persona") for p in data["presets"])
    assert data["categories"], "必须返回分类顺序"


def test_persona_apply_writes_config(client):
    from core import config, persona

    r = client.post("/api/personas/tech/apply")
    assert r.status_code == 200, r.text
    body = r.json()
    cfg = config.get_config()
    assert cfg["llm"]["temperature"] == 0.15
    # tech 带完整角色卡：apply 后写入的是资料卡渲染稿（「老陈」，运维工程师），
    # 不再是 personas.py 里那段简短工作流说明。
    assert body["has_character_card"] is True
    assert "老陈" in cfg["context"]["persona"]
    assert "运维工程师" in cfg["context"]["persona"]

    # 还原：零号也有角色卡，apply zero 写入的是卡片渲染稿而非出厂默认人设；
    # 这里必须直接写回出厂值，否则同进程其它用例拿到的就不是默认配置了。
    config.update_config({"context": {"persona": persona.DEFAULT_PERSONA}})
