"""角色设定一致性：资料卡渲染 + 漂移检出。

这组测试守的是「角色像不像活人」这个需求，具体守四件事：
1. 资料卡能正确渲染（结构化事实在前，空段落不输出空标题）
2. 角色卡坏掉时不拖垮其它角色
3. 通用助手腔能被检出（这是最伤沉浸感的一类）
4. 口癖判据必须准——宁可漏判，不能把符合人设的回复判成漂移
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.character import CharacterCard, load_all, load_card, render_for  # noqa: E402
from core.character_guard import (  # noqa: E402
    check_generic_assistant,
    check_identity,
    check_reply,
    check_voice_signature,
)


def _card(**kw) -> CharacterCard:
    base = dict(id="t", name="测试角色", tagline="一句话定位")
    base.update(kw)
    return CharacterCard(**base)


# ---------------- 资料卡渲染 ----------------

def test_render_puts_facts_before_narrative():
    """结构化事实必须排在最前——越靠前模型越不容易忽略。"""
    c = _card(
        fields={"物种": "猫娘", "住处": "城西"},
        narrative={"identity": "你是一只猫。", "speech": "说话带喵。"},
    )
    out = c.render()
    assert out.index("【设定事实】") < out.index("【身份】")
    assert "物种：猫娘" in out
    assert "住处：城西" in out


def test_render_skips_empty_sections():
    """空字段不能输出空标题，否则模型会以为"这里应该有内容"。"""
    c = _card(fields={}, narrative={"identity": "你是猫。", "world": "", "habits": "  "})
    out = c.render()
    assert "【身份】" in out
    assert "【所处的世界】" not in out
    assert "【习惯与癖好】" not in out


def test_render_includes_unknown_policy_explicitly():
    """资料没覆盖时怎么办，必须显式写进提示词。

    只写「不要编造」是无效的——模型没有"我编了吗"的自我监控。
    要给可执行的话术，这句才管用。
    """
    c = _card(unknown_policy="资料没有的事就说不知道，别编。")
    assert "资料没有的事就说不知道" in c.render()
    assert "【资料没有的事】" in c.render()


def test_voice_samples_labelled_as_tone_not_copy():
    """语料必须标明"学语气不照抄"，否则模型会原样复读。"""
    c = _card(voice_samples=["急什么，我又不欠你。"])
    out = c.render()
    assert "学的是语气和节奏，不是照抄句子" in out


# ---------------- 加载健壮性 ----------------

def test_load_all_skips_broken_file(tmp_path):
    """单个角色卡写坏不能拖垮其它角色——线上是同一目录批量加载。"""
    (tmp_path / "good.json").write_text(
        json.dumps({"id": "good", "name": "好角色", "fields": {"a": "b"}}, ensure_ascii=False),
        encoding="utf-8")
    (tmp_path / "bad.json").write_text("{ this is not json", encoding="utf-8")
    cards = load_all(tmp_path)
    assert "good" in cards
    assert len(cards) == 1


def test_load_card_defaults_when_fields_missing(tmp_path):
    p = tmp_path / "min.json"
    p.write_text('{"id": "min"}', encoding="utf-8")
    c = load_card(p)
    assert c.id == "min"
    assert c.name == "min"          # 没给 name 时回落到文件名
    assert c.fields == {}
    assert c.temperature == 0.8


def test_render_for_missing_character_returns_empty():
    """没有资料卡时返回空串，调用方回退到原 persona，不能抛异常。"""
    assert render_for("__no_such_character__") == ""


def test_all_builtin_characters_render_without_error():
    """仓库里 15 个内置角色都必须能渲染出来。"""
    cards = load_all()
    assert len(cards) >= 15
    for cid, card in cards.items():
        out = card.render()
        assert len(out) > 300, f"{cid} 渲染太短，可能资料缺失"
        assert card.fields, f"{cid} 没有结构化事实"
        assert card.unknown_policy, f"{cid} 没写资料缺失时的处理方式"


# ---------------- 漂移检出 ----------------

def test_generic_assistant_tone_detected():
    """客服腔是最伤沉浸感的一类，必须抓得住。"""
    bad = "作为AI助手，希望这能帮到你。总的来说，这个方案可行。"
    v = check_generic_assistant(bad)
    assert v
    assert all(x.kind == "assistant_tone" for x in v)


def test_clean_reply_has_no_assistant_tone():
    assert check_generic_assistant("行，这事我来办。") == []


def test_identity_drift_detected():
    c = _card(name="零")
    v = check_identity("我只是个程序，本助手无法回答。", c)
    assert v
    assert all(x.kind == "identity_drift" for x in v)


def test_voice_requirement_uses_declared_catchphrases():
    """口癖判据必须来自角色卡显式声明，不能从语料瞎猜。

    早期实现统计语料里重复出现的两字片段，结果：零的标志「喵」是单字，
    完全抓不到；反而把「你这」当口癖，正常回复被误判成漂移。
    """
    c = _card(catchphrases=("喵", "本喵"))
    ok_reply = "开发票这事简单，订单页填个抬头就行，48 小时开出，喵～"
    assert check_voice_signature(ok_reply, c) == []

    bad_reply = "开发票这事简单，订单页填个抬头就行，48 小时就能开出来了。"
    assert check_voice_signature(bad_reply, c)


def test_short_reply_is_not_judged_on_voice():
    """短回复不判口癖——一句「嗯」也可能完全符合人设。"""
    c = _card(catchphrases=("喵",))
    assert check_voice_signature("嗯，好。", c) == []


def test_character_without_catchphrases_never_fails_voice():
    """多数角色不需要固定口癖，不能强行判它们漂移。"""
    c = _card()
    assert check_voice_signature("这是一段足够长的回复内容，用于测试口癖判据不生效的情况。", c) == []


def test_full_check_passes_for_well_formed_reply():
    c = _card(name="零", catchphrases=("喵",))
    r = check_reply("老板你这单子我看了，物流 48 小时能发，喵～", c, strict_voice=True)
    assert r.ok, [v.detail for v in r.violations]


def test_full_check_reports_multiple_kinds():
    """一次回复同时踩多个坑时，要把问题都列出来，便于模型一次改到位。"""
    c = _card(name="零", catchphrases=("喵",))
    r = check_reply("作为AI助手，我只是个程序，希望这能帮到你。", c, strict_voice=True)
    assert not r.ok
    assert {"assistant_tone", "identity_drift"} <= r.kinds
    assert "重说一遍" in r.feedback()


def test_load_all_ignores_unrelated_json_files(tmp_path):
    """目录里的其它 json 不能被当成角色卡。

    回归背景：测试夹具会把 kb.json 写进同一个 tmp_path，盲 glob 把它当角色
    加载，产生了一个幽灵角色。生产环境的角色目录也可能混进配置备份文件。
    """
    (tmp_path / "zero.json").write_text(
        json.dumps({"id": "zero", "name": "零"}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "kb.json").write_text(
        json.dumps({"kb": [{"id": "KB001", "title": "物流"}]}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "config.json").write_text(
        json.dumps({"llm": {"model": "x"}}, ensure_ascii=False), encoding="utf-8")

    cards = load_all(tmp_path)
    assert set(cards) == {"zero"}


# ---------------- 管线接入（_gate_output → character_guard） ----------------

def test_card_for_persona_roundtrip():
    """apply 过的角色提示词必须能反查回角色卡；用户改过的查不到就跳过。"""
    from core.character import card_for_persona, render_for

    rendered = render_for("tech")
    assert card_for_persona(rendered) is not None
    assert card_for_persona(rendered).id == "tech"
    # 微调过的自定义提示词：宁可不校验，也不能拿对不上的卡硬判
    assert card_for_persona(rendered + "\n再补一句。") is None
    assert card_for_persona("") is None


def test_pipeline_gate_combines_card_issues():
    """守卫违规要汇入 _gate_output 的 issue 列表，severity 为 high（触发重写）。"""
    from core.pipeline import _character_issues
    from core.character import render_for
    from core.quality import SEV_HIGH

    persona = render_for("tech")
    # 客服腔 + 身份漂移：tech 卡没有 catchphrases，口癖判据不生效，其余两条都该命中
    issues = _character_issues("作为AI，我只是个程序，希望这能帮到你。", persona)
    codes = {i["code"] for i in issues}
    assert "card_assistant_tone" in codes
    assert "card_identity_drift" in codes
    assert all(i["severity"] == SEV_HIGH for i in issues)


def test_pipeline_gate_skips_when_no_card():
    from core.pipeline import _character_issues

    # 猫娘默认人设不是任何文件卡渲染稿 → 不校验，也不报错
    assert _character_issues("作为AI，我只是个程序。", "你是「自定义」。\n随便写的人设。") == []
    assert _character_issues("什么都行", "") == []
