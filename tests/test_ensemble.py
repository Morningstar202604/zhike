"""多模型择优：规则打分必须把 AI 腔/客服腔挑掉，且不误杀好答案。"""
from core import ensemble


def test_ai_self_scores_way_lower():
    bad = "作为AI助手，我无法回答这个问题。"
    good = "这题我不接，猫生太短，站队的事另请高明，喵～"
    assert ensemble.score_answer(good, "你怎么看中美关系") > ensemble.score_answer(bad, "你怎么看中美关系") + 20


def test_promise_only_penalized():
    assert ensemble.score_answer("我这就去查，稍等。", "今天有什么新闻") < \
        ensemble.score_answer("没查到今天的可靠来源，我不瞎编。", "今天有什么新闻")


def test_ideal_length_bonus():
    ideal = "这事儿吧，得看谁在付钱，反正不是你我说了算，喵～"
    assert ensemble.score_answer(ideal) > ensemble.score_answer("嗯")


def test_empty_candidate_rejected():
    idx, scores = ensemble.choose([{"answer": ""}, {"answer": "好"}])
    assert idx == 1 and scores[0] <= -100


def test_choose_picks_clean_answer():
    cands = [{"answer": "感谢您的咨询，希望这能帮到你"}, {"answer": "行，这事儿我给你办了，喵～"}]
    idx, _ = ensemble.choose(cands, "帮我记住我喜欢喝美式")
    assert idx == 1


def test_choose_empty_list():
    assert ensemble.choose([]) == (-1, [])
