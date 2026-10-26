"""联网搜索：给 Agent 补充实时信息。

设计：
- 默认关闭。开启需要配置搜索提供方（环境变量）。
- 支持三种后端：SearXNG（自建/公共实例）、Tavily、Serper。
- 未配置时返回明确提示，**不伪造搜索结果**。
- 结果经统一裁剪与打码后进入上下文，避免把整页 HTML 塞进 prompt。

配置（任选其一）：
    SEARCH_PROVIDER=searxng|tavily|serper
    SEARXNG_URL=https://searx.example.com
    TAVILY_API_KEY=tvly-xxx
    SERPER_API_KEY=xxx
"""
from __future__ import annotations

import html
import os
import re

import httpx

from core import redact
from core.http_pool import get_client


class SearchError(Exception):
    """搜索失败。上层据此决定是降级还是提示。"""


def provider() -> str:
    p = (os.environ.get("SEARCH_PROVIDER") or "").strip().lower()
    if p:
        return p
    # 未显式指定时按已配置的密钥推断
    if os.environ.get("TAVILY_API_KEY"):
        return "tavily"
    if os.environ.get("SERPER_API_KEY"):
        return "serper"
    if os.environ.get("SEARXNG_URL"):
        return "searxng"
    return ""  # 严格模式：未配置即无搜索。要启用免 Key 通道，显式设 SEARCH_PROVIDER=bing


def available() -> bool:
    return bool(provider())


def _clean(text: str, limit: int = 400) -> str:
    """去掉 HTML 标签与多余空白，截断到 limit。"""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


async def _search_searxng(query: str, top_k: int) -> list[dict]:
    base = (os.environ.get("SEARXNG_URL") or "").rstrip("/")
    if not base:
        raise SearchError("SEARXNG_URL 未配置")
    client = get_client(base, timeout=15)
    resp = await client.get(
        f"{base}/search",
        params={"q": query, "format": "json", "language": "zh-CN"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    out = []
    for item in (data.get("results") or [])[:top_k]:
        out.append({
            "title": _clean(item.get("title", ""), 120),
            "url": item.get("url", ""),
            "snippet": _clean(item.get("content", "")),
        })
    return out


async def _search_tavily(query: str, top_k: int) -> list[dict]:
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        raise SearchError("TAVILY_API_KEY 未配置")
    client = get_client("https://api.tavily.com", timeout=20)
    resp = await client.post(
        "https://api.tavily.com/search",
        json={"api_key": key, "query": query, "max_results": top_k, "search_depth": "basic"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {
            "title": _clean(r.get("title", ""), 120),
            "url": r.get("url", ""),
            "snippet": _clean(r.get("content", "")),
        }
        for r in (data.get("results") or [])[:top_k]
    ]


async def _search_serper(query: str, top_k: int) -> list[dict]:
    key = os.environ.get("SERPER_API_KEY", "").strip()
    if not key:
        raise SearchError("SERPER_API_KEY 未配置")
    client = get_client("https://google.serper.dev", timeout=20)
    resp = await client.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        json={"q": query, "num": top_k},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {
            "title": _clean(r.get("title", ""), 120),
            "url": r.get("link", ""),
            "snippet": _clean(r.get("snippet", "")),
        }
        for r in (data.get("organic") or [])[:top_k]
    ]


_CHATTY_PREFIX = re.compile(
    r"^(?:请|麻烦|帮我|你|能不能|可以)?(?:用)?(?:联网|网上|在线)?(?:帮我)?"
    r"(?:搜一下|搜搜|搜索|搜|查一下|查查|查|看看|看一下|找一下|找找|找)"
    r"(?:一下|下)?[，,：:\s]*")
_CHATTY_SUFFIX = re.compile(
    r"[，,。;；]?\s*(?:给(?:我|出)?(?:来源|链接|出处|原文)|并?注明来源|要有?来源|附上?链接)[。\s]*$")


def clean_query(query: str) -> str:
    """把口语指令洗成检索词。

    模型常把用户原话直接丢给搜索：「联网搜一下今天科技新闻，给出来源」
    在必应里会先去匹配「联网」这个词，返回百科词条——实测就是这样翻车的。
    """
    q = (query or "").strip()
    for _ in range(2):
        q2 = _CHATTY_PREFIX.sub("", q)
        if q2 == q:
            break
        q = q2
    for _ in range(2):
        q2 = _CHATTY_SUFFIX.sub("", q)
        if q2 == q:
            break
        q = q2
    # 时间/疑问类填充词对检索没有区分度，反而把主题词淹掉
    q = re.sub(r"(今天|今日|昨天|明天|现在|最近|最新|有什么|什么|哪些|一下|请问|帮我|我想知道)",
               " ", q)
    q = re.sub(r"\s+", " ", q).strip(" ，,。.;；:：")
    return q or (query or "").strip()


_NEWS_HINT = re.compile(r"新闻|资讯|动态|头条|最新|今天|今日|昨天|本周|热点|发布|进展|融资|发布会")

# 「今天/历史上的今天」类站点对任何含时间词的查询都是天然噪声源（必应会把它们排前列）
_JUNK_DOMAINS = ("rili.com.cn", "lishi.yyxw.com", "lssdjt.com", "todayonhistory.com")


async def _rss_items(url: str, limit: int = 12) -> list[dict]:
    """抓一个 RSS/Atom 源，返回统一结构的条目。失败返回空列表。"""
    try:
        client = get_client(timeout=12)
        resp = await client.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        }, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError:
        return []
    text = resp.text
    out: list[dict] = []
    for chunk in re.findall(r"<item>(.*?)</item>", text, re.S)[:limit]:
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", chunk, re.S)
        l = re.search(r"<link>(.*?)</link>", chunk, re.S)
        d = re.search(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", chunk, re.S)
        p = re.search(r"<pubDate>(.*?)</pubDate>", chunk, re.S)
        if not t:
            continue
        link = (l.group(1).strip() if l else "")
        # Solidot 之类把链接塞在 CDATA 里，原样输出会在前端显示成 <![CDATA[...]]>
        link = re.sub(r"^<!\[CDATA\[|\]\]>$", "", link).strip()
        out.append({
            "title": _clean(html.unescape(t.group(1)), 120),
            "url": link,
            "snippet": _clean(html.unescape(d.group(1)) if d else "", 260),
            "published": (p.group(1).strip() if p else ""),
        })
    return out


# 国内直连可达的科技新闻源（免 Key、无频控）：用于「今天有什么科技新闻」这类
# 时效性问题——网页搜索给的是频道首页，RSS 才给得到当天具体条目。
_NEWS_FEEDS = (
    "https://www.ithome.com/rss/",
    "https://www.solidot.org/index.rss",
    "https://www.chinanews.com.cn/rss/scroll-news.xml",
)


async def _news_feed_results(query: str, top_k: int) -> list[dict]:
    """按关键词从新闻源里筛条目；命中不到就退化为最新几条。

    匹配用 bigram 重叠而不是整句包含：中文没有空格，切词器会把
    「今天AI领域有什么新闻」当成一个词，永远命中不了任何标题。
    """
    import asyncio as _asyncio

    batches = await _asyncio.gather(*[_rss_items(u) for u in _NEWS_FEEDS],
                                    return_exceptions=True)
    items: list[dict] = []
    for b in batches:
        if isinstance(b, list):
            items.extend(b)
    if not items:
        return []
    q = clean_query(query)
    # 去掉时间/疑问类高频词，剩下的实词才算主题
    q = re.sub(r"(今天|今日|昨天|最新|有什么|什么|新闻|资讯|动态|消息|报道|一下|请问)", " ", q)
    words = [w for w in re.split(r"[\s，,。、的了吗呢]+", q) if len(w) >= 2]
    grams = {q[i:i + 2] for i in range(len(q) - 1)} if len(q) >= 2 else set()
    scored: list[tuple[int, dict]] = []
    for it in items:
        blob = f"{it.get('title','')} {it.get('snippet','')}"
        score = sum(2 for w in words if w in blob)
        if grams:
            score += sum(1 for g in grams if g.strip() and g in blob)
        scored.append((score, it))
    scored.sort(key=lambda x: x[0], reverse=True)
    picked = [it for s, it in scored if s > 0][:top_k]
    return picked or [it for _, it in scored[:top_k]]


async def _search_bing(query: str, top_k: int) -> list[dict]:
    """必应（cn.bing.com）HTML 抓取：免 Key 兜底通道（国内直连可达）。

    解析 b_algo 结果块；结构变了就抛 SearchError，上层静默降级。
    时效性问题额外并一路新闻 RSS：网页搜索只能给频道首页，答不了「今天」。
    """
    q = clean_query(query)
    client = get_client("https://cn.bing.com", timeout=20)
    resp = await client.get(
        "https://cn.bing.com/search",
        params={"q": q, "count": str(top_k * 2), "mkt": "zh-CN"},
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        timeout=20,
        follow_redirects=True,
    )
    resp.raise_for_status()
    html_text = resp.text
    out: list[dict] = []
    # b_algo 块：标题链接 + 摘要，正则抓取（不引入 bs4 依赖）
    for chunk in re.findall(r'<li class="b_algo".*?</li>', html_text, re.S)[: top_k * 2]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        if not m:
            continue
        url = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2))
        sm = re.search(r'<p[^>]*>(.*?)</p>', chunk, re.S)
        snippet = re.sub(r"<[^>]+>", "", sm.group(1)) if sm else ""
        if not title.strip():
            continue
        out.append({"title": _clean(title, 120), "url": url, "snippet": _clean(snippet)})
        if len(out) >= top_k:
            break
    if _NEWS_HINT.search(query or ""):
        fresh = await _news_feed_results(query, max(3, top_k // 2))
        if fresh:
            # 新闻条目排前面：时效性问题里「频道首页」等于没答
            seen = {f["url"] for f in fresh}
            out = fresh + [r for r in out if r["url"] not in seen]
    # 日历/历史上的今天这类站是「今天+任意词」的万能匹配噪声，检索里一律剔掉
    out = [r for r in out if not any(d in str(r.get("url") or "") for d in _JUNK_DOMAINS)]
    if not out:
        raise SearchError("必应返回未解析到结果")
    return out[:max(top_k, 1)]


_BACKENDS = {
    "searxng": _search_searxng,
    "tavily": _search_tavily,
    "serper": _search_serper,
    "bing": _search_bing,
}


async def search(query: str, top_k: int = 5) -> dict:
    """执行一次联网搜索。返回 {"ok", "provider", "results", "error"}。

    任何失败都不抛异常——调用方（管线）把 error 写进上下文即可，
    不能让搜索挂了导致整条对话失败。
    """
    q = (query or "").strip()
    if not q:
        return {"ok": False, "provider": "", "results": [], "error": "空查询"}

    prov = provider()
    if not prov:
        return {
            "ok": False, "provider": "", "results": [],
            "error": "未配置搜索服务（SEARCH_PROVIDER / TAVILY_API_KEY / SERPER_API_KEY / SEARXNG_URL）",
        }

    backend = _BACKENDS.get(prov)
    if backend is None:
        return {"ok": False, "provider": prov, "results": [], "error": f"不支持的搜索后端：{prov}"}

    try:
        results = await backend(q, max(1, min(top_k, 10)))
    except SearchError as e:
        return {"ok": False, "provider": prov, "results": [], "error": str(e)}
    except httpx.HTTPError as e:
        return {"ok": False, "provider": prov, "results": [], "error": f"{type(e).__name__}: {str(e)[:120]}"}
    except (ValueError, KeyError) as e:
        return {"ok": False, "provider": prov, "results": [], "error": f"响应解析失败：{type(e).__name__}"}

    # 结果打码：搜索结果里可能带密钥片段（如用户搜索自己的 token）
    results = redact.mask_value(results)
    return {"ok": True, "provider": prov, "results": results, "error": None}


def format_for_context(result: dict, max_chars: int = 1200) -> str:
    """把搜索结果格式化成可放进 prompt 的文本块。"""
    if not result.get("ok"):
        return ""
    lines = []
    budget = max_chars
    for i, r in enumerate(result.get("results") or [], 1):
        line = f"[{i}] {r.get('title', '')}\n{r.get('snippet', '')}\n来源：{r.get('url', '')}"
        if len(line) > budget:
            break
        lines.append(line)
        budget -= len(line)
    return "\n\n".join(lines)
