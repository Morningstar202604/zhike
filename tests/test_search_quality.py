"""联网搜索质量回归：口语指令必须先洗成检索词，否则必应返回百科词条。"""
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
