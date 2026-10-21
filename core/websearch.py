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
    return ""


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


_BACKENDS = {
    "searxng": _search_searxng,
    "tavily": _search_tavily,
    "serper": _search_serper,
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
