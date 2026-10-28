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


def test_builtin_persona_matches_frontend_preset():
    """控制台预设是第二份副本，必须与后端内置角色完全一致。"""
    tsx = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "src" / "BotPersona.tsx"
    source = tsx.read_text(encoding="utf-8")
    m = re.search(r"name: '猫娘·零',.*?persona: `", source, re.S)
    assert m, "BotPersona.tsx 里找不到猫娘预设"
    preset = source[m.end(): source.index("`,", m.end())]
    assert preset == persona.DEFAULT_PERSONA
