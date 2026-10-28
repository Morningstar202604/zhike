"""联网搜索质量回归：口语指令必须先洗成检索词，否则必应返回百科词条。"""
import pytest

from core import websearch


def test_clean_query_strips_chatty_instruction():
    assert websearch.clean_query("联网搜一下今天科技新闻，给出来源") == "科技新闻"
    assert websearch.clean_query("帮我查一下英伟达财报") == "英伟达财报"
    assert websearch.clean_query("搜索 猫粮 推荐") == "猫粮 推荐"


def test_clean_query_keeps_topic_words():
    # 只有时间/疑问词被剥掉，主题词必须保留
    q = websearch.clean_query("今天AI领域有什么新闻")
    assert "AI" in q and "领域" in q
    assert "今天" not in q and "什么" not in q


def test_clean_query_empty_falls_back_to_raw():
    assert websearch.clean_query("搜一下") == "搜一下"
    assert websearch.clean_query("") == ""


def test_rss_items_parse_cdata_links(monkeypatch):
    xml = """<?xml version="1.0"?><rss><channel>
    <item><title><![CDATA[某芯片发布]]></title>
      <link><![CDATA[https://example.com/a]]></link>
      <description><![CDATA[摘要内容]]></description>
      <pubDate>Mon, 26 Oct 2026 10:00:00 +0800</pubDate></item>
    </channel></rss>"""

    class _Resp:
        status_code = 200
        text = xml

        def raise_for_status(self):
            return None

    class _Client:
        async def get(self, url, headers=None, follow_redirects=True):
            return _Resp()

    monkeypatch.setattr(websearch, "get_client", lambda *a, **k: _Client())
    import asyncio

    items = asyncio.run(websearch._rss_items("https://example.com/feed"))
    assert items and items[0]["url"] == "https://example.com/a"
    assert items[0]["title"] == "某芯片发布"
    assert "<![CDATA[" not in items[0]["url"]


def test_vector_hits_below_similarity_floor_are_dropped(monkeypatch):
    """闲聊不能从知识库里捞满无关条目（曾导致「发表情包」答成「发票开具」）。"""
    pytest.importorskip("numpy")
    from core import retrieval

    kb = [{"id": "KB1", "title": "发票开具", "text": "发票 抬头", "tags": []}]
    monkeypatch.setattr(retrieval, "_INDEX_STATE", {"vec": True})
    monkeypatch.setattr(retrieval, "_get_encoder", lambda: type(
        "E", (), {"encode": lambda self, xs, normalize_embeddings=True: [[0.0] * 4]})())
    monkeypatch.setattr(retrieval, "_kb_rowid", lambda eid: 1)

    class _Conn:
        def execute(self, sql, params=None):
            class _R:
                def fetchall(self):
                    # distance=1.3 → cos = 1-1.69/2 = 0.155 < 0.48
                    return [{"rowid": 1, "distance": 1.3}]
            return _R()

    assert retrieval._vec_search(_Conn(), "发个表情包", kb, 4) == []


def test_filler_words_do_not_drive_retrieval():
    """问法里的虚词不能当主题词：曾因「怎么」命中「产品使用指引」。"""
    from core import retrieval

    assert "怎么" not in retrieval.strip_fillers("你怎么看中美关系")
    assert "怎么" not in retrieval.strip_fillers("这个怎么用")
    # 实词必须保留
    assert "退货" in retrieval.strip_fillers("退货政策是什么")


def test_generic_question_does_not_hit_kb():
    """纯闲聊/政治题不应该命中商品知识库。"""
    from core import retrieval

    assert retrieval.retrieve("你怎么看中美关系", top_k=3, min_overlap=0.08) == []
    assert retrieval.retrieve("你觉得现在这个世界谁说了算", top_k=3, min_overlap=0.08) == []


def test_business_question_still_hits():
    from core import retrieval

    hits = retrieval.retrieve("我的订单什么时候发货", top_k=3, min_overlap=0.08)
    assert hits and hits[0]["id"] == "KB001"
