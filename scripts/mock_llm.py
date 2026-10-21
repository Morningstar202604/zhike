"""本地 Mock LLM：OpenAI 兼容 /v1/chat/completions + /v1/models，纯标准库零依赖。

用途：没有真实 LLM Key 时验通 知客 的 LLM 管线（react 解析/工具路由/降级）。

用法：
    python scripts/mock_llm.py [port]        # 默认 9099
    # 然后把 LLM Base URL 指到 http://127.0.0.1:<port>/v1，Model 填 mock-chat，
    # API Key 随便填非空即可（管理台「LLM 配置」或 POST /api/config）。

行为：按消息里是否出现 订单号/退款/物流 关键词，返回结构化 JSON 决策；
含 "格式坏掉" 的回答由 _parse_llm_answer 兜底，不在此模拟。
"""
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9099

MARKDOWN_SAMPLE = "\n".join([
    "## 退货政策",
    "",
    "支持 **7 天无理由**退货，需满足：",
    "",
    "- 商品未拆封",
    "- 保留原包装",
    "",
    "| 情形 | 处理 |",
    "|---|---|",
    "| 质量问题 | 全额退 |",
    "| 无理由 | 扣运费 |",
    "",
    "```python",
    "def refund(order_id: str) -> bool:",
    "    return True",
    "```",
    "",
    "详情见 [帮助中心](https://example.com/help)。",
])


def _last_user(messages: list) -> str:
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            return m.get("content", "") or ""
    return ""


def _decide(query: str) -> dict:
    m = re.search(r"(?:订单|order|单号)\s*[:：]?\s*([A-Za-z0-9]{6,12})", query, re.IGNORECASE)
    if m and ("退款" in query or "退货" in query):
        return {"tool": "initiate_refund", "tool_args": {"order_id": m.group(1), "reason": "mock-llm"}}
    if m:
        return {"tool": "query_order", "tool_args": {"order_id": m.group(1)}}
    return {"tool": None, "tool_args": None}


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": "mock-chat", "object": "model"}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": {"message": "not found"}})
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        query = _last_user(req.get("messages"))
        decision = _decide(query)
        if "xss" in query.lower():
            content = json.dumps({
                "thought": "",
                "answer": "正常文本<script>window.__xss=1</script><img src=x onerror=\"window.__xss=2\">[坏链接](javascript:alert(1))",
                "citations": [], "confidence": 0.5, "escalate": False,
                "escalate_reason": "", "tool": None, "tool_args": None,
            }, ensure_ascii=False)
            self._json(200, {
                "id": "mock-xss", "object": "chat.completion", "model": "mock-chat",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })
            return
        if "markdown" in query.lower():
            content = json.dumps({
                "thought": "mock: 返回富文本样例",
                "answer": MARKDOWN_SAMPLE,
                "citations": ["KB002"],
                "confidence": 0.93,
                "escalate": False,
                "escalate_reason": "",
                "tool": None,
                "tool_args": None,
            }, ensure_ascii=False)
            self._json(200, {
                "id": f"mockchat-{time.time():.0f}",
                "object": "chat.completion",
                "model": req.get("model", "mock-chat"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })
            return
        content = json.dumps({
            "thought": "mock: 关键词路由",
            "answer": f"【Mock LLM】已收到您的问题「{query[:40]}」，这是模拟回答。",
            "citations": ["KB001"] if any(k in query for k in ("发货", "物流", "快递")) else [],
            "confidence": 0.9,
            "escalate": False,
            "escalate_reason": "",
            **decision,
        }, ensure_ascii=False)
        self._json(200, {
            "id": f"mockchat-{time.time():.0f}",
            "object": "chat.completion",
            "model": req.get("model", "mock-chat"),
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def log_message(self, *args):  # 静默，避免污染核心日志
        pass


if __name__ == "__main__":
    print(f"[mock-llm] OpenAI 兼容端点已就绪: http://127.0.0.1:{PORT}/v1", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
