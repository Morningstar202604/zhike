"""FastAPI 核心：访客 Web API（旧版前端兼容）+ 渠道摄取 /v1/ingest + 坐席工作台 + 管理面。"""
import asyncio
import json
import logging
import os
import re
import secrets
import contextlib
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from core import config, events_log, executor_client, guardrail, llm, memory as memory_mod, net, pipeline, ratelimit, retrieval, security, sessions, store, tools as tools_mod
from core.actions import authz as actions_authz
from core.actions import client as actions_client
from core.actions import registry as actions_registry
from core.actions import service as actions_service

logger = logging.getLogger("core")
ev = logging.getLogger("cs.events")

# ---- 入参体积护栏（模块级常量，便于配置与测试覆盖）----
# 图片 base64 上限（约 6MB 原始图；与 llm._build_messages 的可接受范围一致）
MAX_IMAGE_B64_CHARS = int(os.environ.get("MAX_IMAGE_B64_CHARS", str(8_000_000)))
# 单条文本硬上限（pipeline 还会按 max_message_len 截断，这里挡住恶意超大 payload）
MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", "20000"))
# 知识库批量导入单次条目数上限
MAX_KB_IMPORT_ITEMS = int(os.environ.get("MAX_KB_IMPORT_ITEMS", "2000"))


def _parse_kb_blocks(raw: str) -> list[tuple[str, str]]:
    """批量导入文本 → [(标题, 答案)]，自动识别两种格式：

    - 标题模式：以 ## / ### 开头的行为条目标题，其后所有内容（可含空行）为答案
    - 分块模式：空行分隔的块，块内首行为标题，其余行为答案
    """
    raw = (raw or "").replace("\r\n", "\n").strip()
    if not raw:
        return []
    lines = raw.split("\n")
    blocks: list[tuple[str, str]] = []
    if any(ln.lstrip().startswith(("## ", "### ")) for ln in lines):
        title, body = None, []
        for ln in lines:
            ls = ln.strip()
            if ls.startswith(("## ", "### ")):
                if title is not None:
                    blocks.append((title, "\n".join(body).strip()))
                title, body = ls.lstrip("#").strip(), []
            elif title is not None:
                body.append(ln)
        if title is not None:
            blocks.append((title, "\n".join(body).strip()))
    else:
        title, body = None, []
        for ln in lines:
            if not ln.strip():
                if title is not None:
                    blocks.append((title, "\n".join(body).strip()))
                    title, body = None, []
            elif title is None:
                title = ln.strip()
            else:
                body.append(ln)
        if title is not None:
            blocks.append((title, "\n".join(body).strip()))
    return [(t, a) for t, a in blocks if t]


class Hub:
    def __init__(self):
        self.visitor_conns: dict[str, set] = {}
        self.operator_conns: set = set()

    # 单连接发送超时：卡住的客户端不能拖住整批广播。
    # 取 2 秒：正常 WS 发送是毫秒级，2 秒足够排完本地缓冲；
    # 真卡住的连接直接放弃并清理，不让它拖累其他用户。
    SEND_TIMEOUT_S = 2.0

    async def _send_one(self, ws, payload: dict, timeout: float) -> bool:
        """给单个连接发消息，返回是否成功。超时/异常都算失败（会被清理）。"""
        try:
            await asyncio.wait_for(ws.send_json(payload), timeout=timeout)
            return True
        except (asyncio.TimeoutError, Exception):
            return False

    async def publish(self, session_id: str, payload: dict):
        """推送给某个会话的所有访客连接（并发发送）。

        原实现串行 await：一个慢客户端会阻塞其余所有连接
        （实测 11 个连接、其中 1 个卡 3 秒 → 整批耗时 3150ms）。
        改为 gather 并发 + 单连接超时。
        """
        conns = list(self.visitor_conns.get(session_id, ()))
        if not conns:
            return
        results = await asyncio.gather(
            *(self._send_one(ws, payload, self.SEND_TIMEOUT_S) for ws in conns),
            return_exceptions=True,
        )
        for ws, ok in zip(conns, results):
            if ok is not True:
                self.visitor_conns.get(session_id, set()).discard(ws)

    async def broadcast_to_operators(self, payload: dict):
        """广播给所有坐席连接（并发发送，单个失败不影响其他人）。"""
        conns = list(self.operator_conns)
        if not conns:
            return
        results = await asyncio.gather(
            *(self._send_one(ws, payload, self.SEND_TIMEOUT_S) for ws in conns),
            return_exceptions=True,
        )
        for ws, ok in zip(conns, results):
            if ok is not True:
                self.operator_conns.discard(ws)

    async def _hold(self, ws: WebSocket, on_close):
        """挂住连接直到断开。任何异常路径都必须执行清理，否则死连接会永久占用注册表。"""
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception as e:  # 客户端异常断开/协议错误：同样要清理
            ev.info("ws closed abnormally: %s: %s", type(e).__name__, str(e)[:80])
        finally:
            on_close()

    async def connect(self, session_id: str, ws: WebSocket, already_accepted: bool = False):
        if not already_accepted:
            await ws.accept()
        self.visitor_conns.setdefault(session_id, set()).add(ws)
        await self._hold(ws, lambda: self.visitor_conns.get(session_id, set()).discard(ws))

    async def connect_operator(self, ws: WebSocket, already_accepted: bool = False):
        if not already_accepted:
            await ws.accept()
        self.operator_conns.add(ws)
        await self._hold(ws, lambda: self.operator_conns.discard(ws))


hub = Hub()


def require_admin(x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
    token = security.admin_token()
    if not token:
        # 安全默认：ADMIN_TOKEN 未设置时管理接口一律禁用（不再开发模式裸奔）
        raise HTTPException(
            503,
            "ADMIN_TOKEN 未配置：管理接口已禁用。请在环境变量设置强随机令牌（openssl rand -hex 32）后重启。",
        )
    if not security.constant_time_equals(x_admin_token, token):
        raise HTTPException(401, "需要管理员令牌")


def require_channel(x_channel_token: str | None = Header(default=None, alias="X-Channel-Token")):
    """渠道端点共享密钥：未设置 CHANNEL_TOKEN 时直接 503，防公网伪造消息/刷 LLM 成本。"""
    token = os.environ.get("CHANNEL_TOKEN", "").strip()
    if not token:
        raise HTTPException(
            503,
            "CHANNEL_TOKEN 未配置：渠道摄取已禁用。请在环境变量设置强随机令牌后重启。",
        )
    if not security.constant_time_equals(x_channel_token, token):
        raise HTTPException(401, "需要渠道令牌")


def _require_session_secret(sid: str, x_session_secret: str | None = Header(default=None, alias="X-Session-Secret")):
    """web 访客会话归属校验：sid + secret 双因子。渠道会话（QQ 等，由 CHANNEL_TOKEN 保护）不经过此依赖。"""
    if not store.check_session_secret(sid, x_session_secret or ""):
        raise HTTPException(401, "会话无效或归属校验失败")


def _require_admin_or_session(
    sid: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    x_session_secret: str | None = Header(default=None, alias="X-Session-Secret"),
):
    """只读会话内容的双重授权：管理员令牌（控制台/坐席查看任意会话）或访客自己的 secret。

    渠道会话（QQ）创建时没有 secret，坐席台必须靠管理员令牌才能回放对话；
    写操作（发消息/升级/反馈）仍只认 secret，避免拿管理员令牌代替访客身份。
    """
    token = security.admin_token()
    if token and security.constant_time_equals(x_admin_token, token):
        return
    if not store.check_session_secret(sid, x_session_secret or ""):
        raise HTTPException(401, "会话无效或需要管理员令牌")


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    if retrieval.seed_kb_if_missing():
        logger.info("knowledge base seeded to %s", retrieval.KB_PATH)
    applied = net.apply()
    logger.info("DNS overrides applied: %s (LLM_HOSTS set=%s)", applied, bool(os.environ.get("LLM_HOSTS")))
    events_log.attach()
    app.state.gateway_channels = {}
    if not security.admin_token():
        logger.warning(
            "ADMIN_TOKEN 未设置：所有 /api/admin/* 与坐席接口已禁用（503）。生产部署必须设置。"
        )
    if not os.environ.get("CHANNEL_TOKEN", "").strip():
        logger.warning(
            "CHANNEL_TOKEN 未设置：/v1/ingest 与网关心跳已禁用（503）。生产部署必须设置。"
        )
    app.state.limiter = ratelimit.SQLiteRateLimiter()
    yield
    # 退出时关闭共享 HTTP 连接池，避免资源泄漏
    try:
        from core import http_pool

        await http_pool.close_all()
    except Exception:
        pass


def create_app() -> FastAPI:
    app = FastAPI(title="Zhike CS Agent Core", lifespan=lifespan)
    cors_env = os.environ.get("CS_AGENT_CORS_ORIGINS", "").strip()
    if cors_env:
        origins = [o.strip() for o in cors_env.split(",") if o.strip()]
    else:
        # 安全默认：不再 * 全开。开发模式默认放行 vite dev server；生产必须显式配置。
        origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "X-Admin-Token", "X-Channel-Token", "X-Session-Secret", "X-Agent-Token", "Content-Type"],
    )

    class NewSession(BaseModel):
        visitor_id: str = "anonymous"

    class MessageIn(BaseModel):
        content: str
        image: str | None = None
        # 联网搜索：由前端开关传入
        web_search: bool = False

    class FeedbackIn(BaseModel):
        message_id: int | None = None
        rating: str
        comment: str = ""

    class ConfigIn(BaseModel):
        config: dict

    class ToolIn(BaseModel):
        id: str
        name: str = ""
        desc: str = ""
        args_json: dict = {}
        enabled: bool = True

    class RuleIn(BaseModel):
        expr: str
        action: str = "block"
        enabled: bool = True

    class KbIn(BaseModel):
        id: str
        text: str = ""
        title: str = ""
        answer: str = ""
        tags: list[str] = []

    class KbImportIn(BaseModel):
        text: str
        replace: bool = False

    class KbTestIn(BaseModel):
        query: str

    class IngestIn(BaseModel):
        channel_type: str
        channel_user_id: str
        group_id: str | None = None
        text: str
        display_name: str = ""
        image_b64: str | None = None

    # ---- 入参体积护栏 ----
    def _check_payload(text: str | None, image_b64: str | None) -> None:
        if text and len(text) > MAX_TEXT_CHARS:
            raise HTTPException(413, f"文本过长（上限 {MAX_TEXT_CHARS} 字符）")
        if image_b64:
            if len(image_b64) > MAX_IMAGE_B64_CHARS:
                raise HTTPException(413, f"图片过大（base64 上限 {MAX_IMAGE_B64_CHARS} 字符）")
            if image_b64.startswith("data:") and "," not in image_b64:
                raise HTTPException(400, "图片 data URL 格式不合法")

    class HeartbeatIn(BaseModel):
        gateway: str
        status: str = "online"
        detail: dict = {}

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        """兜底：任何未预料的异常都要落日志并返回结构化 JSON，而不是让客户端拿到 traceback。"""
        ev.error("unhandled %s %s: %s: %s",
                 request.method, request.url.path, type(exc).__name__, str(exc)[:300])
        return JSONResponse(
            status_code=500,
            content={"detail": "服务内部错误，已记录日志"},
        )

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    def _preflight(sid: str, text: str, visitor_key: str) -> None:
        """入口侧注入检测与审计落库。所有入口（渠道/非流式/流式）都必须先过一次。"""
        inj, inj_action = guardrail.detect_injection(text or "")
        if inj:
            store.log_injection(sid, visitor_key, text, inj, inj_action or "block")
            if (inj_action or "block") == "block":
                ev.warning(f"guardrail block visitor={visitor_key} hit={inj!r}")

    def _remember(visitor_key: str, text: str, reply) -> None:
        """写入最近话题与偏好沉淀。

        两种情况必须跳过：
        1. 本轮就是 opt-out 语句（否则把"别记我"又存回去）
        2. 该访客此前已 opt-out（持久化标记，否则会话内还会继续记录）
        """
        if not text or getattr(reply, "opt_out", False):
            return
        if store.memory_opted_out(visitor_key):
            return
        store.set_memory(visitor_key, "last_topic", text[:50])
        memory_mod.capture(text, visitor_key, config.get_config())

    def _safe_reply(error: Exception) -> "pipeline.AgentReply":
        """管线内部异常时的兜底回复。

        用户不该因为一次模型/网络故障看到 500，也不能看到沉默——统一转人工并留日志。
        """
        ev.error("pipeline failed: %s: %s", type(error).__name__, str(error)[:200])
        return pipeline.AgentReply(
            text="抱歉，刚才处理时出了点问题，已为您转接人工坐席。",
            confidence=0.0,
            escalate=True,
            escalate_reason="pipeline_error",
            mode="fallback",
        )

    async def _handle_inbound(sid: str, text: str, image: str | None, visitor_key: str,
                              web_search: bool = False):
        _preflight(sid, text, visitor_key)
        ev.info(f"ingest visitor={visitor_key} text={text[:60]}")
        # 管线只需要最近若干轮：全量读取会让长会话的上下文构建从 18ms 涨到 37ms。
        # 取 60 条（约 30 轮）足够 history_turns 默认 10 轮 + 相关性筛选回溯。
        history = store.get_messages(sid, limit=60, include_deleted=False)
        visitor_msg_id = store.add_message(sid, "visitor", text or "（附图）")
        try:
            reply = await pipeline.run(
                pipeline.ChannelMessage(text=text, image_b64=image or "", web_search=web_search),
                history,
                visitor_key,
            )
        except Exception as e:  # 管线内部异常不能变成 500，也不能让访客干等
            reply = _safe_reply(e)
        msg_id = store.add_message(sid, "agent", reply.text, refs=reply.citations,
                                   tool_used=reply.tool_used, thought=reply.thought)
        ev.info(
            f"reply session={sid[:8]} mode={reply.mode} tool={reply.tool_used} "
            f"escalate={reply.escalate} citations={reply.citations}"
        )
        ticket_id = None
        if reply.escalate:
            store.mark_escalated(sid, reply.escalate_reason or "escalate")
            ticket_id = store.create_ticket(sid)
            ev.warning(f"escalate session={sid[:8]} reason={reply.escalate_reason} ticket={ticket_id}")
            await hub.broadcast_to_operators(
                {"type": "new_pending", "session_id": sid, "ticket_id": ticket_id, "reason": reply.escalate_reason}
            )
        _remember(visitor_key, text, reply)
        return reply, msg_id, ticket_id, visitor_msg_id

    @app.post("/v1/ingest", dependencies=[Depends(require_channel)])
    async def ingest(body: IngestIn):
        text = (body.text or "").strip()
        if not text and not body.image_b64:
            raise HTTPException(400, "empty message")
        _check_payload(text, body.image_b64)
        identity = f"{body.channel_type}:{body.channel_user_id}"
        limiter: security.RateLimiter = app.state.limiter
        if not limiter.allow(identity):
            raise HTTPException(429, "rate limited")
        chat_key = body.group_id or body.channel_user_id
        sid = sessions.resolve_session(f"{body.channel_type}:{chat_key}", identity)
        reply, msg_id, ticket_id, _visitor_msg_id = await _handle_inbound(
            sid, text, body.image_b64, identity)
        return {
            "session_id": sid,
            "message_id": msg_id,
            "ticket_id": ticket_id,
            "reply": {
                "text": reply.text,
                "citations": reply.citations,
                "confidence": reply.confidence,
                "escalate": reply.escalate,
                "escalate_reason": reply.escalate_reason,
                "mode": reply.mode,
                "tool_used": reply.tool_used,
                "tool_result": reply.tool_result,
                "thought": reply.thought,
            },
        }

    @app.post("/api/sessions")
    def new_session(body: NewSession):
        sid = uuid.uuid4().hex
        secret = secrets.token_urlsafe(24)
        store.create_session(sid, body.visitor_id, secret=secret)
        return {"session_id": sid, "session_secret": secret, "status": "ongoing", "visitor_id": body.visitor_id}

    @app.get("/api/sessions/{sid}/messages")
    def get_messages(sid: str, _=Depends(_require_admin_or_session)):
        return {"messages": store.get_messages(sid)}

    @app.delete("/api/sessions/{sid}/messages/{message_id}")
    def delete_message(sid: str, message_id: int, _=Depends(_require_session_secret)):
        """访客删除自己会话里的一条消息（软删除）。

        物理删除会破坏审计与投诉取证；软删除后消息不再返回、
        也不会进入模型上下文（get_messages 仍会带 deleted 标记供前端灰显）。
        """
        if not store.delete_message(sid, message_id):
            raise HTTPException(404, "消息不存在或已删除")
        return {"ok": True, "message_id": message_id}

    @app.post("/api/sessions/{sid}/messages")
    async def post_message(sid: str, body: MessageIn, request: Request, _=Depends(_require_session_secret)):
        content = (body.content or "").strip()
        if not content and not body.image:
            raise HTTPException(400, "empty message")
        _check_payload(content, body.image)
        sess = store.get_session(sid)
        if sess and sess["status"] == "closed":
            raise HTTPException(409, "会话已关闭，请开始新会话")
        visitor_id = (sess or {}).get("visitor_id", "anonymous")
        limiter: security.RateLimiter = app.state.limiter
        # 限流 key 用客户端 IP（不再用可伪造的 visitor_id），防一人刷爆全匿名桶
        ip_key = f"ip:{request.client.host if request.client else 'unknown'}"
        if not limiter.allow(ip_key):
            raise HTTPException(429, "rate limited")
        reply, msg_id, ticket_id, visitor_msg_id = await _handle_inbound(
            sid, content, body.image, visitor_id, web_search=body.web_search)
        return {
            "role": "agent",
            "text": reply.text,
            "citations": reply.citations,
            "confidence": reply.confidence,
            "escalate": reply.escalate,
            "escalate_reason": reply.escalate_reason,
            "ticket_id": ticket_id,
            "mode": reply.mode,
            "tool_used": reply.tool_used,
            "tool_result": reply.tool_result,
            "memory_note": reply.memory_note,
            "ctx_used": reply.ctx_used,
            "thought": reply.thought,
            "message_id": msg_id,
            "visitor_message_id": visitor_msg_id,
        }

    def _sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    @app.post("/api/sessions/{sid}/chat/stream")
    async def chat_stream(sid: str, body: MessageIn, request: Request, _=Depends(_require_session_secret)):
        """访客流式问答（SSE 范式：delta 增量打字机 → done 权威终态，终态 text 为准）。"""
        content = (body.content or "").strip()
        if not content and not body.image:
            raise HTTPException(400, "empty message")
        _check_payload(content, body.image)
        sess = store.get_session(sid)
        visitor_id = (sess or {}).get("visitor_id", "anonymous")
        limiter: security.RateLimiter = app.state.limiter
        ip_key = f"ip:{request.client.host if request.client else 'unknown'}"
        if not limiter.allow(ip_key):
            raise HTTPException(429, "rate limited")
        if not sess:
            raise HTTPException(404, "session not found")
        if sess["status"] == "closed":
            raise HTTPException(409, "会话已关闭，请开始新会话")

        async def gen():
            yield _sse("meta", {"session_id": sid})
            _preflight(sid, content, visitor_id)
            ev.info(f"ingest(visitor stream) visitor={visitor_id} text={content[:60]}")
            history = store.get_messages(sid, limit=60, include_deleted=False)
            visitor_msg_id = store.add_message(sid, "visitor", content or "（附图）")
            yield _sse("ack", {"visitor_message_id": visitor_msg_id})
            reply, msg_id, ticket_id = None, None, None
            try:
                async for kind, payload in pipeline.run_stream(
                    pipeline.ChannelMessage(text=content, image_b64=body.image or "",
                                            web_search=body.web_search),
                    history,
                    visitor_id,
                ):
                    if kind == "delta":
                        yield _sse("delta", {"text": payload})
                    else:
                        reply = payload
            except Exception as e:  # 流式过程中断：不能把 None 传到下面去
                reply = _safe_reply(e)
            if reply is None:
                # run_stream 正常结束却没产出 final（契约破坏或空实现）：退回安全话术，不让请求 500
                reply = _safe_reply(RuntimeError("pipeline stream produced no final reply"))
            msg_id = store.add_message(sid, "agent", reply.text, reply.citations,
                                       tool_used=reply.tool_used, thought=reply.thought)
            if reply.escalate:
                store.mark_escalated(sid, reply.escalate_reason)
                ticket_id = store.create_ticket(sid)
                ev.info(f"escalate session={sid} reason={reply.escalate_reason} ticket={ticket_id}")
                await hub.broadcast_to_operators(
                    {"type": "new_pending", "session_id": sid, "ticket_id": ticket_id, "reason": reply.escalate_reason}
                )
            _remember(visitor_id, content, reply)
            yield _sse("done", {
                "role": "agent",
                "text": reply.text,
                "citations": reply.citations,
                "confidence": reply.confidence,
                "escalate": reply.escalate,
                "escalate_reason": reply.escalate_reason,
                "ticket_id": ticket_id,
                "mode": reply.mode,
                "tool_used": reply.tool_used,
                "tool_result": reply.tool_result,
                "memory_note": reply.memory_note,
                "ctx_used": reply.ctx_used,
                "thought": reply.thought,
                "message_id": msg_id,
                "visitor_message_id": visitor_msg_id,
            })

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/sessions/{sid}/escalate")
    async def escalate(sid: str, _=Depends(_require_session_secret)):
        sess = store.get_session(sid)
        if not sess:
            raise HTTPException(404, "会话不存在")
        if sess["status"] == "closed":
            raise HTTPException(409, "会话已关闭")
        store.mark_escalated(sid, "visitor_requested")
        ticket_id = store.create_ticket(sid)
        store.add_message(sid, "agent", "好的，正在为您转接人工坐席。")
        await hub.broadcast_to_operators(
            {"type": "new_pending", "session_id": sid, "ticket_id": ticket_id, "reason": "visitor_requested"}
        )
        return {"ticket_id": ticket_id, "status": "pending_agent"}

    @app.get("/api/operator/queues", dependencies=[Depends(require_admin)])
    def operator_queues():
        return store.pending_queues()

    @app.post("/api/operator/take/{sid}", dependencies=[Depends(require_admin)])
    async def take_session(sid: str, body: NewSession | None = None):
        if body is None:
            body = NewSession()
        if not store.get_session(sid):
            raise HTTPException(404, "会话不存在")
        store.set_status(sid, "agent_handled", assignee=body.visitor_id)
        await hub.publish(sid, {"type": "agent_handled", "operator": body.visitor_id, "session_id": sid})
        await hub.broadcast_to_operators({"type": "session_updated", "session_id": sid, "status": "agent_handled"})
        return {"session_id": sid, "status": "agent_handled", "assignee": body.visitor_id}

    @app.post("/api/operator/reply/{sid}", dependencies=[Depends(require_admin)])
    async def operator_reply(sid: str, body: MessageIn):
        content = body.content.strip()
        if not content:
            raise HTTPException(400, "empty message")
        if not store.get_session(sid):
            raise HTTPException(404, "会话不存在")
        store.add_message(sid, "operator", content)
        await hub.publish(
            sid,
            {"type": "operator_message", "role": "operator", "content": content, "session_id": sid},
        )
        await hub.broadcast_to_operators({"type": "operator_replied", "session_id": sid, "content": content})
        return {"role": "operator", "content": content}

    @app.post("/api/operator/close/{sid}", dependencies=[Depends(require_admin)])
    async def close_session(sid: str):
        if not store.get_session(sid):
            raise HTTPException(404, "会话不存在")
        store.close_session(sid)
        await hub.publish(sid, {"type": "session_closed", "session_id": sid})
        await hub.broadcast_to_operators({"type": "session_updated", "session_id": sid, "status": "closed"})
        return {"session_id": sid, "status": "closed"}

    @app.get("/api/memory/{visitor_id}", dependencies=[Depends(require_admin)])
    def get_memory(visitor_id: str):
        return {
            "visitor_id": visitor_id,
            "memory": store.get_memory(visitor_id),
            "visits": store.session_count(visitor_id),
        }

    @app.post("/api/sessions/{sid}/feedback")
    def add_feedback(sid: str, body: FeedbackIn, _=Depends(_require_session_secret)):
        if body.rating not in ("helpful", "not_helpful"):
            raise HTTPException(400, "rating must be helpful or not_helpful")
        # message_id 必须属于本会话且是 agent 消息，防止给他人消息打分
        if body.message_id:
            msgs = store.get_messages(sid)
            target = next((m for m in msgs if m["id"] == body.message_id), None)
            if target is None:
                raise HTTPException(400, "message_id 不属于该会话")
            if target["role"] != "agent":
                raise HTTPException(400, "只能对 Agent 回复评分")
        store.add_feedback(sid, body.message_id or 0, body.rating, body.comment)
        if body.rating == "not_helpful":
            sess = store.get_session(sid)
            if sess and sess["status"] == "ongoing":
                store.mark_escalated(sid, "feedback_not_helpful")
                ticket_id = store.create_ticket(sid)
                store.add_message(sid, "agent", "很抱歉回答未帮到您，已为您转接人工坐席。", tool_used="feedback")
                return {"rating": body.rating, "escalated": True, "ticket_id": ticket_id}
        return {"rating": body.rating, "escalated": False}

    @app.get("/api/feedback/summary", dependencies=[Depends(require_admin)])
    def feedback_summary():
        return store.feedback_summary()

    @app.get("/api/agent/health", dependencies=[Depends(require_admin)])
    def agent_health():
        cfg = config.get_config()
        llm_cfg = cfg.get("llm", {})
        engine_mode = cfg.get("engine", {}).get("mode", "auto")
        effective = "llm_react" if (engine_mode in ("llm", "auto") and llm_cfg.get("has_key")) else "rule_react"
        active = tools_mod.active_tools(cfg)
        # 脱敏：不返回 base_url 等内部拓扑；model 名是界面展示必需（总览页要显示引擎型号），且 /api/config 本就返回
        return {
            "mode": effective,
            "engine_mode": engine_mode,
            "llm_ready": bool(llm_cfg.get("has_key")),
            "model": llm_cfg.get("model", "") if llm_cfg.get("has_key") else "",
            "kb_entries": len(retrieval.load_kb()),
            "tool_count": len(active),
            "injection_rules": len(store.list_injection_rules(enabled_only=True)),
            "kb_top_k": cfg.get("retrieval", {}).get("kb_top_k"),
            "history_turns": cfg.get("context", {}).get("history_turns"),
            "memory_enable": cfg.get("memory", {}).get("enable"),
            "escalate_threshold": cfg.get("escalation", {}).get("confidence_threshold"),
            "react_steps": cfg.get("escalation", {}).get("react_steps"),
        }

    @app.get("/api/config", dependencies=[Depends(require_admin)])
    def get_config():
        import copy as _copy

        cfg = config.get_config()
        key = cfg["llm"].get("api_key") or ""
        view = _copy.deepcopy(cfg)
        view["llm"]["api_key"] = ("••••" + key[-4:]) if key else ""
        return {"config": view}

    @app.put("/api/config", dependencies=[Depends(require_admin)])
    def update_config(body: ConfigIn):
        import copy as _copy

        allowed = set(config.DEFAULTS.keys())
        patch = {k: v for k, v in (body.config or {}).items() if k in allowed}
        try:
            cfg = config.update_config(patch)
        except ValueError as e:
            raise HTTPException(400, str(e))
        key = cfg["llm"].get("api_key") or ""
        view = _copy.deepcopy(cfg)
        view["llm"]["api_key"] = ("••••" + key[-4:]) if key else ""
        return {"config": view}

    def _validate_tool_args(args_json: dict) -> None:
        if not isinstance(args_json, dict):
            raise HTTPException(400, "args_json 必须是参数名→类型的对象")
        for k in args_json:
            if not isinstance(k, str) or not k.isidentifier():
                raise HTTPException(400, f"参数名 '{k}' 非法")

    @app.get("/api/tools", dependencies=[Depends(require_admin)])
    def list_tools():
        return {"tools": store.list_tools()}

    @app.post("/api/tools", dependencies=[Depends(require_admin)])
    def create_tool(body: ToolIn):
        _validate_tool_args(body.args_json)
        builtin = 1 if body.id in ("query_order", "initiate_refund", "lookup_policy") else 0
        try:
            t = store.upsert_tool(body.id, body.name, body.desc, body.args_json, body.enabled, builtin)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"tool": t}

    @app.put("/api/tools/{tool_id}", dependencies=[Depends(require_admin)])
    def update_tool(tool_id: str, body: ToolIn):
        _validate_tool_args(body.args_json)
        existing = store.get_tool(tool_id)
        if not existing:
            raise HTTPException(404, "tool not found")
        try:
            t = store.upsert_tool(tool_id, body.name or tool_id, body.desc, body.args_json, body.enabled, existing["builtin"])
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"tool": t}

    @app.delete("/api/tools/{tool_id}", dependencies=[Depends(require_admin)])
    def delete_tool(tool_id: str):
        if not store.delete_tool(tool_id):
            raise HTTPException(404, "tool not found")
        return {"ok": True}

    @app.get("/api/injection-rules", dependencies=[Depends(require_admin)])
    def list_rules():
        return {"rules": store.list_injection_rules()}

    def _check_rule(body: RuleIn):
        import re as _re

        if len(body.expr) > 300:
            raise HTTPException(400, "正则表达式过长（≤300 字符，防回溯型正则拖慢消息管线）")
        try:
            _re.compile(body.expr, _re.IGNORECASE)
        except _re.error as e:
            raise HTTPException(400, f"正则表达式非法：{e}")
        if body.action not in ("block", "log_only"):
            raise HTTPException(400, "action 必须是 block 或 log_only")

    @app.post("/api/injection-rules", dependencies=[Depends(require_admin)])
    def create_rule(body: RuleIn):
        _check_rule(body)
        return {"rule": store.add_injection_rule(body.expr, body.action, body.enabled)}

    @app.put("/api/injection-rules/{rule_id}", dependencies=[Depends(require_admin)])
    def update_rule(rule_id: int, body: RuleIn):
        _check_rule(body)
        r = store.update_injection_rule(rule_id, body.expr, body.action, body.enabled)
        if not r:
            raise HTTPException(404, "rule not found")
        return {"rule": r}

    @app.delete("/api/injection-rules/{rule_id}", dependencies=[Depends(require_admin)])
    def delete_rule(rule_id: int):
        if not store.delete_injection_rule(rule_id):
            raise HTTPException(404, "rule not found")
        return {"ok": True}

    @app.get("/api/kb", dependencies=[Depends(require_admin)])
    def kb_list():
        emb_flag = config.get_config().get("retrieval", {}).get("embedding", "auto")
        return {
            "kb": retrieval.load_kb(),
            "embedding": {
                "available": retrieval.embedding_available(),
                "enabled": emb_flag,
                "model": retrieval.BGE_MODEL if retrieval.embedding_available() else "",
            },
        }

    _kb_lock = threading.Lock()

    @contextlib.contextmanager
    def _kb_file_lock():
        """跨进程文件锁。

        只用 threading.Lock 不够：多 worker（uvicorn --workers N）或多实例部署时
        每个进程有自己的锁对象，彼此不互斥，仍会丢更新（实测 5 进程各写 10 条只剩 12 条）。
        这里用操作系统级独占锁，Linux/macOS 走 fcntl，Windows 走 msvcrt。
        """
        lock_path = retrieval.KB_PATH.with_suffix(retrieval.KB_PATH.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                # 阻塞等待：重试直到拿到锁
                while True:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()

    def _kb_mutate(fn):
        """在【进程内锁 + 跨进程文件锁】内完成「读 → 改 → 写」。

        两层锁缺一不可：
        - threading.Lock：同进程多线程互斥
        - 文件锁：多 worker/多实例互斥
        只锁写盘不行，必须把读取也纳入临界区，否则读到旧列表后覆盖（实测丢 78% 数据）。
        """
        with _kb_lock, _kb_file_lock():
            # 强制绕过缓存从磁盘读，拿到的才是最新内容
            retrieval._KB_CACHE = None
            items = list(retrieval.load_kb())
            result, new_items = fn(items)
            _save_kb_unlocked(new_items)
            retrieval._KB_CACHE = None
            return result, new_items

    def _save_kb_unlocked(items: list[dict]) -> None:
        """原子写知识库。

        并发安全靠三件事：
        1. 模块级锁串行化「读-改-写」，避免两个管理员同时改导致丢更新
        2. 临时文件名带 uuid，避免并发抢同一个 .tmp（Windows 上会 WinError 32）
        3. os.replace 失败时重试（杀毒/索引器会短暂占用句柄）
        """
        p = retrieval.KB_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"kb": items}, f, ensure_ascii=False, indent=2)
        last_err = None
        for attempt in range(3):
            try:
                os.replace(tmp, p)
                break
            except OSError as e:
                last_err = e
                time.sleep(0.05 * (attempt + 1))
        else:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise RuntimeError(f"知识库写入失败：{type(last_err).__name__}") from last_err

    @app.post("/api/kb", dependencies=[Depends(require_admin)])
    def add_kb(body: KbIn):
        def op(items):
            if any(e["id"] == body.id for e in items):
                raise HTTPException(400, "kb id already exists")
            items.append({"id": body.id, "text": body.text, "title": body.title,
                          "answer": body.answer, "tags": body.tags})
            return None, items
        _, items = _kb_mutate(op)
        return {"kb": items}

    @app.put("/api/kb/{kb_id}", dependencies=[Depends(require_admin)])
    def update_kb(kb_id: str, body: KbIn):
        def op(items):
            for i, e in enumerate(items):
                if e["id"] == kb_id:
                    items[i] = {"id": kb_id, "text": body.text, "title": body.title,
                                "answer": body.answer, "tags": body.tags}
                    return None, items
            raise HTTPException(404, "kb not found")
        _, items = _kb_mutate(op)
        return {"kb": items}

    @app.delete("/api/kb/{kb_id}", dependencies=[Depends(require_admin)])
    def delete_kb(kb_id: str):
        def op(items):
            new = [e for e in items if e["id"] != kb_id]
            if len(new) == len(items):
                raise HTTPException(404, "kb not found")
            return None, new
        _, new = _kb_mutate(op)
        return {"kb": new}

    @app.post("/api/kb/import", dependencies=[Depends(require_admin)])
    def import_kb(body: KbImportIn):
        """批量导入知识库：自动识别「## 标题」或空行分块两种格式，条目 id 自动编号（KB101 起）。

        replace=True 时清空现有条目后导入（整库重建）。答案里行首出现「## 」会破坏再导入，避免使用。
        """
        blocks = _parse_kb_blocks(body.text)
        if not blocks:
            raise HTTPException(400, "未能解析出任何条目：用「## 标题」开头，或用空行分隔「标题/正文」块")
        if len(blocks) > MAX_KB_IMPORT_ITEMS:
            raise HTTPException(413, f"单次导入条目过多（{len(blocks)} > {MAX_KB_IMPORT_ITEMS}），请分批导入")
        def op(items):
            if body.replace:
                items = []
            base = _next_kb_base(items)
            for title, answer in blocks:
                base += 1
                items.append({
                    "id": f"KB{base}",
                    "title": title[:80],
                    "text": f"{title} {answer}"[:300],
                    "answer": answer or title,
                    "tags": [],
                })
            return None, items
        _, items = _kb_mutate(op)
        return {"added": len(blocks), "total": len(items), "kb": items}

    @app.post("/api/kb/import/document", dependencies=[Depends(require_admin)])
    async def import_kb_document(file: UploadFile):
        """文档导入管道：PDF/Word/Excel/Markdown → 解析 → 标题/段落分块 → KB 条目（id 续号，追加语义）。

        PDF/DOCX/XLSX 解析为可选依赖（requirements-docs.txt），缺失时 400 给出安装提示。
        """
        from core import docparse

        data = await file.read()
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(400, "文档超过 10MB 上限")
        fname = file.filename or "doc"
        try:
            text = docparse.extract_text(fname, data)
            blocks = docparse.chunk_document(text, fname.rsplit(".", 1)[0])
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        if not blocks:
            raise HTTPException(400, "未能从文档中解析出内容")
        def op(items):
            base = _next_kb_base(items)
            for title, chunk in blocks:
                base += 1
                items.append({
                    "id": f"KB{base}",
                    "title": title[:80],
                    "text": f"{title} {chunk}"[:300],
                    "answer": chunk,
                    "tags": ["文档"],
                })
            return None, items
        _, items = _kb_mutate(op)
        return {"added": len(blocks), "total": len(items), "file": fname}

    @app.post("/api/kb/test", dependencies=[Depends(require_admin)])
    def kb_test(body: KbTestIn):
        """试召回：按当前生效配置（top-K/门槛/向量开关）跑一次检索，返回命中与分数。"""
        q = (body.query or "").strip()
        if not q:
            raise HTTPException(400, "empty query")
        cfg = config.get_config()
        top_k = cfg.get("retrieval", {}).get("kb_top_k", 4)
        min_overlap = cfg.get("retrieval", {}).get("min_overlap", 0.0)
        emb_flag = cfg.get("retrieval", {}).get("embedding", "auto")
        use_emb = None if emb_flag == "auto" else (emb_flag == "on")
        hits = retrieval.retrieve_scored(q, top_k, min_overlap, use_embedding=use_emb)
        return {
            "query": q,
            "top_k": top_k,
            "min_overlap": min_overlap,
            "embedding": emb_flag,
            "hits": [
                {"id": e["id"], "title": e["title"], "score": round(s, 3), "answer": (e["answer"] or "")[:120]}
                for s, e in hits
            ],
        }

    def _next_kb_base(items: list[dict]) -> int:
        base = 100
        for e in items:
            m = re.match(r"^KB(\d+)$", e["id"])
            if m:
                base = max(base, int(m.group(1)))
        return base

    @app.get("/api/kb/export", dependencies=[Depends(require_admin)])
    def export_kb():
        """导出全部知识库为 Markdown（「## 标题」格式，可直接再导入，形成备份闭环）。"""
        from fastapi.responses import PlainTextResponse

        lines: list[str] = []
        for e in retrieval.load_kb():
            lines.append(f"## {e['title']}")
            lines.append(e["answer"] or e["text"])
            lines.append("")
        return PlainTextResponse(
            "\n".join(lines).strip() + "\n",
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=zhike-kb.md"},
        )

    @app.get("/api/admin/overview", dependencies=[Depends(require_admin)])
    def admin_overview():
        with store.get_conn() as conn:
            sessions_count = conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
            messages = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
            tickets = conn.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"]
            pending = conn.execute("SELECT COUNT(*) c FROM sessions WHERE status='pending_agent'").fetchone()["c"]
        return {
            "sessions": sessions_count,
            "messages": messages,
            "tickets": tickets,
            "pending": pending,
            "feedback": store.feedback_summary(),
            "kb_count": len(retrieval.load_kb()),
            "injection": len(store.injection_log(50)),
        }

    @app.get("/api/admin/stats", dependencies=[Depends(require_admin)])
    def admin_stats():
        return store.admin_stats()

    @app.get("/api/admin/report", dependencies=[Depends(require_admin)])
    def admin_report(days: int = 7):
        """经营报表：近 N 天日序列 + 命中率/升级率/CSAT 汇总。"""
        return store.admin_report(days)

    class KbSaveNoteIn(BaseModel):
        title: str = ""
        text: str
        tags: list[str] = []

    @app.post("/api/kb/save-note", dependencies=[Depends(require_admin)])
    def kb_save_note(body: KbSaveNoteIn):
        """坐席答案回流：把坐席手工回复存为 KB 条目（id 续号，tag 坐席回流），沉淀为下次的自动答案。"""
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(400, "empty text")
        title = (body.title or text.strip().splitlines()[0] or text[:30]).strip()[:80]

        def op(items):
            base = _next_kb_base(items)
            entry = {
                "id": f"KB{base + 1}",
                "title": title,
                "text": f"{title} {text}"[:300],
                "answer": text,
                "tags": body.tags or ["坐席回流"],
            }
            items.append(entry)
            return entry, items
        entry, items = _kb_mutate(op)
        return {"entry": entry, "total": len(items)}

    @app.post("/v1/gateway/heartbeat", dependencies=[Depends(require_channel)])
    def gateway_heartbeat(body: HeartbeatIn):
        app.state.gateway_channels[body.gateway] = {
            "status": body.status,
            "last_seen": time.time(),
            "detail": body.detail or {},
        }
        return {"ok": True}

    @app.get("/api/admin/sessions", dependencies=[Depends(require_admin)])
    def admin_sessions(status: str | None = None, limit: int = 50, offset: int = 0,
                       q: str | None = None):
        return store.admin_sessions(limit=limit, offset=offset, status=status, q=q)

    @app.get("/api/admin/channels", dependencies=[Depends(require_admin)])
    def admin_channels():
        channels = [{"channel": "webchat", "status": "builtin", "last_seen": None, "detail": {}}]
        gateways = dict(app.state.gateway_channels)
        for name, info in sorted(gateways.items()):
            status = info.get("status", "offline")
            last = info.get("last_seen")
            if status == "online" and last and time.time() - last > 90:
                status = "stale"
            channels.append({
                "channel": name,
                "status": status,
                "last_seen": last,
                "detail": info.get("detail", {}),
            })
        if not any(c["channel"] == "onebot_v11" for c in channels):
            channels.append({"channel": "onebot_v11", "status": "offline", "last_seen": None, "detail": {}})
        return {"channels": channels}

    @app.get("/api/admin/logs", dependencies=[Depends(require_admin)])
    def admin_logs(limit: int = 200, level: str | None = None):
        return {"entries": events_log.entries(limit=limit, level=level)}

    @app.post("/api/admin/llm-test", dependencies=[Depends(require_admin)])
    async def llm_test():
        return await llm.probe()

    @app.get("/api/admin/llm-models", dependencies=[Depends(require_admin)])
    async def llm_models():
        return await llm.list_models()

    @app.get("/api/admin/injection-log", dependencies=[Depends(require_admin)])
    def admin_injection_log():
        return {"entries": store.injection_log(50)}

    @app.get("/api/admin/memory", dependencies=[Depends(require_admin)])
    def admin_memory():
        return {"entries": store.all_memory()}

    # ============================================================
    # 动作层：动作目录 / 执行 / 审计 / 审批
    # ============================================================

    class ActionExecuteIn(BaseModel):
        action: str
        params: dict = {}
        session_id: str | None = None
        idempotency_key: str | None = None
        approval_id: str | None = None
        # 由谁触发：admin / operator:xxx / qq:123 / agent@qq:123
        # 不传时按管理员令牌推断为 admin（控制台直接调用）
        principal: str | None = None

    class ApprovalIn(BaseModel):
        reason: str = ""

    def _principal_from(body_principal: str | None, x_admin_token: str | None,
                        x_agent_token: str | None = None) -> str:
        """决定本次调用的主体。**绝不信任请求体自报的身份。**

        规则（按优先级）：
        1. 带合法 AGENT_TOKEN → 允许声明 agent@<origin>（网关/Agent 服务间调用）
        2. 带合法 ADMIN_TOKEN → admin
        3. 否则 → 匿名 qq 主体（只读之外一律拒绝）

        为什么不能直接用 body.principal：那样任何匿名请求写
        `{"principal":"admin"}` 就能拿到管理员权限（已复现）。
        """
        token = security.admin_token()
        is_admin = bool(token) and security.constant_time_equals(x_admin_token, token)

        agent_token = os.environ.get("AGENT_TOKEN", "").strip()
        is_agent = bool(agent_token) and security.constant_time_equals(x_agent_token, agent_token)

        if is_agent:
            # 服务间调用：允许传 agent@<origin>，但仍限制为 agent 主体
            if body_principal and body_principal.startswith("agent"):
                return body_principal
            return "agent"
        if is_admin:
            # 管理员可以声明"以某个 QQ 身份触发"用于测试，但降级为 agent 语义
            if body_principal and body_principal.startswith("agent"):
                return body_principal
            return "admin"
        # 未认证：忽略任何自报身份
        return "qq:anonymous"

    @app.get("/api/actions/catalog", dependencies=[Depends(require_admin)])
    def actions_catalog():
        return {
            "actions": actions_registry.catalog(),
            "risks": actions_authz.risk_summary(),
            "agent_tier": actions_authz.agent_tier(),
            "tier_label": actions_authz.TIER_LABEL[actions_authz.agent_tier()],
            "l2_enabled": actions_authz.l2_enabled(),
        }

    @app.post("/api/actions/execute")
    async def actions_execute(body: ActionExecuteIn,
                              x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
                              x_agent_token: str | None = Header(default=None, alias="X-Agent-Token")):
        """执行动作。

        权限由 principal 决定，不依赖管理员令牌本身——
        带管理员令牌只说明"调用方是控制台"，具体能做什么仍走策略判定。
        """
        principal = _principal_from(body.principal, x_admin_token, x_agent_token)
        try:
            result = await actions_service.execute(
                action=body.action,
                params=body.params or {},
                principal_raw=principal,
                session_id=body.session_id,
                idempotency_key=body.idempotency_key,
                approval_id=body.approval_id,
            )
        except actions_service.ActionError as e:
            raise HTTPException(e.status, str(e), headers=None)
        return result

    @app.get("/api/actions/logs", dependencies=[Depends(require_admin)])
    def actions_logs(limit: int = 100, status: str | None = None,
                     action: str | None = None, risk: int | None = None):
        return {"entries": store.action_log_list(limit=limit, status=status, action=action, risk=risk)}

    @app.get("/api/approvals", dependencies=[Depends(require_admin)])
    def approvals_list(status: str = "pending"):
        return {"entries": store.approval_list(status=status)}

    @app.get("/api/approvals/{approval_id}", dependencies=[Depends(require_admin)])
    def approval_detail(approval_id: str):
        ap = store.approval_get(approval_id)
        if not ap:
            raise HTTPException(404, "审批不存在")
        return ap

    @app.post("/api/approvals/{approval_id}/approve", dependencies=[Depends(require_admin)])
    def approval_approve(approval_id: str):
        try:
            return actions_service.approve(approval_id, decided_by="admin")
        except actions_service.ActionError as e:
            raise HTTPException(e.status, str(e))

    @app.post("/api/approvals/{approval_id}/deny", dependencies=[Depends(require_admin)])
    def approval_deny(approval_id: str, body: ApprovalIn | None = None):
        try:
            return actions_service.deny(approval_id, decided_by="admin",
                                        reason=(body.reason if body else ""))
        except actions_service.ActionError as e:
            raise HTTPException(e.status, str(e))

    @app.get("/api/admin/audit/verify", dependencies=[Depends(require_admin)])
    def audit_verify(from_id: int | None = None):
        return store.verify_audit_chain(from_id)

    @app.get("/api/admin/audit/entries", dependencies=[Depends(require_admin)])
    def audit_entries(limit: int = 100):
        with store.get_conn() as conn:
            rows = conn.execute(
                "SELECT id, ts, actor, kind, payload, hash FROM audit_chain ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return {"entries": [dict(r) for r in rows]}

    @app.get("/api/executor/health", dependencies=[Depends(require_admin)])
    async def executor_health():
        """电脑执行器健康。核心侧只做 HTTP 探测，不在本进程执行任何本地命令。"""
        return await executor_client.health()

    @app.get("/api/gateway/health", dependencies=[Depends(require_admin)])
    async def gateway_health():
        """QQ 网关健康（协议端是否已连接）。"""
        return await actions_client.probe()

    @app.get("/api/admin/schema", dependencies=[Depends(require_admin)])
    def admin_schema():
        """数据库迁移版本与各表容量。"""
        return {"schema_version": store.schema_version(), "tables": store.data_stats()}

    @app.post("/api/admin/purge", dependencies=[Depends(require_admin)])
    def admin_purge(days: int = 180):
        """清理超过保留期的运营数据。

        审计链 audit_chain 永不删除（哈希链完整性），只清理可再生的运营表。
        """
        try:
            removed = store.purge_old_data(days)
        except ValueError as e:
            raise HTTPException(400, str(e))
        store.append_audit("admin", "data.purge", {"days": days, "removed": removed})
        return {"days": days, "removed": removed}

    @app.get("/api/admin/outbox", dependencies=[Depends(require_admin)])
    def admin_outbox():
        return {"stats": store.outbox_stats()}

    @app.get("/api/websearch/status")
    def websearch_status():
        """联网搜索可用性（前端用它决定是否显示开关）。"""
        from core import websearch

        return {
            "available": websearch.available(),
            "provider": websearch.provider(),
            "hint": None if websearch.available() else (
                "未配置搜索服务。设置 SEARCH_PROVIDER + 对应密钥后启用："
                "TAVILY_API_KEY / SERPER_API_KEY / SEARXNG_URL"
            ),
        }

    @app.get("/api/channels/adapters", dependencies=[Depends(require_admin)])
    def channel_adapters():
        """已注册的渠道适配器与能力矩阵。

        未接入网关时返回空列表——不假装有渠道。
        """
        try:
            from gateway.adapters.bus import registry as gw_registry

            return {"adapters": gw_registry.describe()}
        except Exception:
            return {"adapters": []}

    # ============================================================
    # Agent 多步任务（ReAct）
    # ============================================================

    class AgentRunIn(BaseModel):
        goal: str
        principal: str | None = None
        session_id: str | None = None
        max_steps: int | None = None
        # true 时立即返回 run_id，任务在后台跑；控制台据此轮询进度并支持取消。
        # 默认 false 保持同步语义（脚本/自动化调用可直接拿结果）。
        background: bool = False

    # 运行中的取消信号：run_id -> threading.Event
    # 单 worker 部署下够用；多 worker 时改为查库标志（见计划阶段 3）
    app.state.run_cancels = {}
    # 后台任务句柄：避免 asyncio 回收未完成的任务（fire-and-forget 会被 GC 掉）
    app.state.run_tasks = {}

    @app.post("/api/agent/run")
    async def agent_run(body: AgentRunIn,
                        x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
                        x_agent_token: str | None = Header(default=None, alias="X-Agent-Token")):
        """跑一次多步任务。goal 是自然语言目标。

        与 /api/actions/execute 的区别：那个执行单个动作，这个让模型自己规划多步。
        """
        if not body.goal.strip():
            raise HTTPException(400, "goal 不能为空")
        principal_raw = _principal_from(body.principal, x_admin_token, x_agent_token)

        # 预算可由请求覆盖步数（其余仍走环境变量）
        budget = {}
        if body.max_steps is not None:
            budget["max_steps"] = max(1, min(int(body.max_steps), 20))

        import threading
        import uuid as _uuid

        from core import agent_llm, agent_loop

        cancel = threading.Event()
        run_id = _uuid.uuid4().hex
        # 先登记再启动：这样任务一开跑就能被取消，不必等 run_id 回传
        app.state.run_cancels[run_id] = cancel

        async def llm_call(messages, tools):
            return await agent_llm.call_llm(messages, tools)

        async def execute_run():
            try:
                return await agent_loop.run_agent(
                    goal=body.goal.strip(),
                    principal_raw=principal_raw,
                    llm_call=llm_call,
                    session_id=body.session_id,
                    budget=budget or None,
                    cancel_event=cancel,
                    run_id=run_id,
                )
            finally:
                app.state.run_cancels.pop(run_id, None)

        if body.background:
            # 立即返回：多步任务最长可跑 120s，同步等待会让反向代理超时，
            # 而且 run_id 要到跑完才拿到，取消接口在 UI 上等于不存在（已复现）。
            task = asyncio.create_task(execute_run())
            app.state.run_tasks[run_id] = task

            def _cleanup(t, _rid=run_id):
                app.state.run_tasks.pop(_rid, None)
                if t.cancelled():
                    return
                exc = t.exception()
                if exc:  # 后台任务的异常无人 await，必须自己记日志
                    ev.error("background agent run failed: %s: %s",
                             type(exc).__name__, str(exc)[:200])

            task.add_done_callback(_cleanup)
            return {
                "run_id": run_id, "status": "running", "answer": "",
                "usage": {"steps": 0, "tool_calls": 0, "tokens": 0, "cost": 0.0},
                "steps": [], "error": "",
            }

        try:
            result = await execute_run()
        except Exception as e:
            ev.error("agent run failed: %s: %s", type(e).__name__, str(e)[:200])
            raise HTTPException(500, f"任务执行失败：{type(e).__name__}")

        return {
            "run_id": result.run_id,
            "status": result.status,
            "answer": result.answer,
            "usage": result.usage,
            "steps": [
                {"step": s.step_no, "thought": s.thought, "action": s.action,
                 "status": s.status, "duration_ms": s.duration_ms,
                 "observation": (s.observation or "")[:300]}
                for s in result.steps
            ],
            "error": result.error,
        }

    @app.get("/api/agent/runs", dependencies=[Depends(require_admin)])
    def agent_runs(limit: int = 50, status: str | None = None):
        return {"entries": store.agent_runs_list(limit=limit, status=status)}

    @app.get("/api/agent/runs/{run_id}", dependencies=[Depends(require_admin)])
    def agent_run_detail(run_id: str):
        run = store.agent_run_get(run_id)
        if not run:
            raise HTTPException(404, "任务不存在")
        return {"run": run, "steps": store.agent_steps_list(run_id)}

    @app.post("/api/agent/runs/{run_id}/cancel", dependencies=[Depends(require_admin)])
    def agent_run_cancel(run_id: str):
        """取消一个运行中的任务。在下一个检查点生效（不强行中断正在执行的动作）。"""
        evt = app.state.run_cancels.get(run_id)
        if evt is None:
            run = store.agent_run_get(run_id)
            if not run:
                raise HTTPException(404, "任务不存在")
            if run["status"] != "running":
                raise HTTPException(409, f"任务已结束（{run['status']}）")
            raise HTTPException(409, "任务不在本进程运行（可能已转移到其他 worker）")
        evt.set()
        return {"run_id": run_id, "status": "cancelling"}

    @app.get("/api/debug/conn", dependencies=[Depends(require_admin)])
    def debug_conn():
        return {
            "visitor_sessions": list(hub.visitor_conns.keys()),
            "visitor_total": sum(len(v) for v in hub.visitor_conns.values()),
            "operator_total": len(hub.operator_conns),
        }

    @app.websocket("/ws/operator")
    async def ws_operator(ws: WebSocket):
        # 管理端实时推送：通过 Sec-WebSocket-Protocol 子协议传 token（避免 query string 进 access log / 浏览器历史）
        # 客户端写法：new WebSocket(url, ["zhike-admin", token])
        token = security.admin_token()
        if not token:
            await ws.close(code=4401)
            return
        subprotocols = ws.headers.get("sec-websocket-protocol", "")
        parts = [p.strip() for p in subprotocols.split(",")]
        # 约定 ["zhike-admin", "<token>"]
        client_token = parts[1] if len(parts) >= 2 else ""
        if not security.constant_time_equals(client_token, token):
            await ws.close(code=4401)
            return
        # 握手时必须回选 subprotocol，否则浏览器认为握手失败
        await ws.accept(subprotocol="zhike-admin")
        await hub.connect_operator(ws, already_accepted=True)

    @app.websocket("/ws/{sid}")
    async def ws(sid: str, ws: WebSocket, secret: str | None = None):
        # 访客 WS：校验 session secret（query string，因为 secret 是会话级短期凭据，且仅本会话可读）
        if not store.check_session_secret(sid, secret or ""):
            await ws.close(code=4401)
            return
        await hub.connect(sid, ws)

    # ---- 单端口模式：可选托管前端构建产物（CS_AGENT_STATIC_DIR=frontend/dist）----
    static_dir = os.environ.get("CS_AGENT_STATIC_DIR", "").strip()
    if static_dir and os.path.isdir(static_dir):
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        index_html = os.path.join(static_dir, "index.html")
        assets_dir = os.path.join(static_dir, "assets")
        static_root = os.path.realpath(static_dir)
        if os.path.isdir(assets_dir):
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str):
            if full_path.startswith(("api/", "v1/", "ws/", "docs", "openapi.json", "healthz")):
                raise HTTPException(404, "not found")
            # realpath 包含性校验：拒绝 ../ 逃出静态目录（防路径穿越读取任意文件）
            candidate = os.path.realpath(os.path.join(static_dir, full_path))
            if full_path and candidate.startswith(static_root + os.sep) and os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(index_html)

    return app


app = create_app()
