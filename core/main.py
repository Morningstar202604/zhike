"""FastAPI 核心：访客 Web API（旧版前端兼容）+ 渠道摄取 /v1/ingest + 坐席工作台 + 管理面。"""
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core import config, events_log, guardrail, llm, memory as memory_mod, net, pipeline, retrieval, security, sessions, store, tools as tools_mod

logger = logging.getLogger("core")
ev = logging.getLogger("cs.events")


class Hub:
    def __init__(self):
        self.visitor_conns: dict[str, set] = {}
        self.operator_conns: set = set()

    async def publish(self, session_id: str, payload: dict):
        for ws in list(self.visitor_conns.get(session_id, ())):
            try:
                await ws.send_json(payload)
            except Exception:  # aqg: top-level boundary
                self.visitor_conns.get(session_id, set()).discard(ws)

    async def broadcast_to_operators(self, payload: dict):
        for ws in list(self.operator_conns):
            try:
                await ws.send_json(payload)
            except Exception:  # aqg: top-level boundary
                self.operator_conns.discard(ws)

    async def connect(self, session_id: str, ws: WebSocket):
        await ws.accept()
        self.visitor_conns.setdefault(session_id, set()).add(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            self.visitor_conns.get(session_id, set()).discard(ws)

    async def connect_operator(self, ws: WebSocket):
        await ws.accept()
        self.operator_conns.add(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            self.operator_conns.discard(ws)


hub = Hub()


def require_admin(x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
    token = security.admin_token()
    if token and x_admin_token != token:
        raise HTTPException(401, "需要管理员令牌")


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    applied = net.apply()
    logger.info("DNS overrides applied: %s (LLM_HOSTS set=%s)", applied, bool(os.environ.get("LLM_HOSTS")))
    events_log.attach()
    app.state.gateway_channels = {}
    if not security.admin_token():
        logger.warning(
            "ADMIN_TOKEN 未设置：操作员/管理接口无需令牌（开发模式）。生产部署必须设置 ADMIN_TOKEN。"
        )
    app.state.limiter = security.RateLimiter()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Zhike CS Agent Core", lifespan=lifespan)
    cors_env = os.environ.get("CS_AGENT_CORS_ORIGINS", "").strip()
    origins = [o.strip() for o in cors_env.split(",") if o.strip()] if cors_env else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    class NewSession(BaseModel):
        visitor_id: str = "anonymous"

    class MessageIn(BaseModel):
        content: str
        image: str | None = None

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

    class IngestIn(BaseModel):
        channel_type: str
        channel_user_id: str
        group_id: str | None = None
        text: str
        display_name: str = ""
        image_b64: str | None = None

    class HeartbeatIn(BaseModel):
        gateway: str
        status: str = "online"
        detail: dict = {}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    async def _handle_inbound(sid: str, text: str, image: str | None, visitor_key: str):
        inj, inj_action = guardrail.detect_injection(text)
        if inj:
            store.log_injection(sid, visitor_key, text, inj, inj_action or "block")
            if (inj_action or "block") == "block":
                ev.warning(f"guardrail block visitor={visitor_key} hit={inj!r}")
        ev.info(f"ingest visitor={visitor_key} text={text[:60]}")
        history = store.get_messages(sid)
        store.add_message(sid, "visitor", text or "（附图）")
        reply = await pipeline.run(
            pipeline.ChannelMessage(text=text, image_b64=image or ""),
            history,
            visitor_key,
        )
        msg_id = store.add_message(sid, "agent", reply.text, refs=reply.citations, tool_used=reply.tool_used)
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
        if text:
            store.set_memory(visitor_key, "last_topic", text[:50])
            memory_mod.capture(text, visitor_key, config.get_config())
        return reply, msg_id, ticket_id

    @app.post("/v1/ingest")
    async def ingest(body: IngestIn):
        text = (body.text or "").strip()
        if not text and not body.image_b64:
            raise HTTPException(400, "empty message")
        identity = f"{body.channel_type}:{body.channel_user_id}"
        limiter: security.RateLimiter = app.state.limiter
        if not limiter.allow(identity):
            raise HTTPException(429, "rate limited")
        chat_key = body.group_id or body.channel_user_id
        sid = sessions.resolve_session(f"{body.channel_type}:{chat_key}", identity)
        reply, msg_id, ticket_id = await _handle_inbound(sid, text, body.image_b64, identity)
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
            },
        }

    @app.post("/api/sessions")
    def new_session(body: NewSession):
        sid = uuid.uuid4().hex
        store.create_session(sid, body.visitor_id)
        return {"session_id": sid, "status": "ongoing", "visitor_id": body.visitor_id}

    @app.get("/api/sessions/{sid}/messages")
    def get_messages(sid: str):
        return {"messages": store.get_messages(sid)}

    @app.post("/api/sessions/{sid}/messages")
    async def post_message(sid: str, body: MessageIn):
        content = (body.content or "").strip()
        if not content and not body.image:
            raise HTTPException(400, "empty message")
        sess = store.get_session(sid)
        visitor_id = (sess or {}).get("visitor_id", "anonymous")
        limiter: security.RateLimiter = app.state.limiter
        if not limiter.allow(visitor_id):
            raise HTTPException(429, "rate limited")
        reply, msg_id, _ = await _handle_inbound(sid, content, body.image, visitor_id)
        return {
            "role": "agent",
            "text": reply.text,
            "citations": reply.citations,
            "confidence": reply.confidence,
            "escalate": reply.escalate,
            "escalate_reason": reply.escalate_reason,
            "ticket_id": None,
            "mode": reply.mode,
            "tool_used": reply.tool_used,
            "tool_result": reply.tool_result,
            "memory_note": reply.memory_note,
            "ctx_used": reply.ctx_used,
            "message_id": msg_id,
        }

    @app.post("/api/sessions/{sid}/escalate")
    async def escalate(sid: str):
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
        store.set_status(sid, "agent_handled", assignee=body.visitor_id)
        await hub.publish(sid, {"type": "agent_handled", "operator": body.visitor_id, "session_id": sid})
        await hub.broadcast_to_operators({"type": "session_updated", "session_id": sid, "status": "agent_handled"})
        return {"session_id": sid, "status": "agent_handled", "assignee": body.visitor_id}

    @app.post("/api/operator/reply/{sid}", dependencies=[Depends(require_admin)])
    async def operator_reply(sid: str, body: MessageIn):
        content = body.content.strip()
        if not content:
            raise HTTPException(400, "empty message")
        store.add_message(sid, "operator", content)
        await hub.publish(
            sid,
            {"type": "operator_message", "role": "operator", "content": content, "session_id": sid},
        )
        await hub.broadcast_to_operators({"type": "operator_replied", "session_id": sid, "content": content})
        return {"role": "operator", "content": content}

    @app.post("/api/operator/close/{sid}", dependencies=[Depends(require_admin)])
    async def close_session(sid: str):
        store.close_session(sid)
        await hub.publish(sid, {"type": "session_closed", "session_id": sid})
        await hub.broadcast_to_operators({"type": "session_updated", "session_id": sid, "status": "closed"})
        return {"session_id": sid, "status": "closed"}

    @app.get("/api/memory/{visitor_id}")
    def get_memory(visitor_id: str):
        return {
            "visitor_id": visitor_id,
            "memory": store.get_memory(visitor_id),
            "visits": store.session_count(visitor_id),
        }

    @app.post("/api/sessions/{sid}/feedback")
    def add_feedback(sid: str, body: FeedbackIn):
        if body.rating not in ("helpful", "not_helpful"):
            raise HTTPException(400, "rating must be helpful or not_helpful")
        store.add_feedback(sid, body.message_id or 0, body.rating, body.comment)
        if body.rating == "not_helpful":
            sess = store.get_session(sid)
            if sess and sess["status"] == "ongoing":
                store.mark_escalated(sid, "feedback_not_helpful")
                ticket_id = store.create_ticket(sid)
                store.add_message(sid, "agent", "很抱歉回答未帮到您，已为您转接人工坐席。", tool_used="feedback")
                return {"rating": body.rating, "escalated": True, "ticket_id": ticket_id}
        return {"rating": body.rating, "escalated": False}

    @app.get("/api/feedback/summary")
    def feedback_summary():
        return store.feedback_summary()

    @app.get("/api/agent/health")
    def agent_health():
        cfg = config.get_config()
        llm_cfg = cfg.get("llm", {})
        engine_mode = cfg.get("engine", {}).get("mode", "auto")
        effective = "llm_react" if (engine_mode in ("llm", "auto") and llm_cfg.get("has_key")) else "rule_react"
        active = tools_mod.active_tools(cfg)
        return {
            "mode": effective,
            "engine_mode": engine_mode,
            "llm_ready": bool(llm_cfg.get("has_key")),
            "model": llm_cfg.get("model"),
            "base_url": llm_cfg.get("base_url"),
            "kb_entries": len(retrieval.load_kb()),
            "tools": list(active.keys()),
            "tool_count": len(active),
            "injection_rules": len(store.list_injection_rules(enabled_only=True)),
            "kb_top_k": cfg.get("retrieval", {}).get("kb_top_k"),
            "history_turns": cfg.get("context", {}).get("history_turns"),
            "memory_enable": cfg.get("memory", {}).get("enable"),
            "escalate_threshold": cfg.get("escalation", {}).get("confidence_threshold"),
            "react_steps": cfg.get("escalation", {}).get("react_steps"),
            "dns_overrides": net.status(),
        }

    @app.get("/api/config")
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
        cfg = config.update_config(patch)
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

    @app.get("/api/kb")
    def kb_list():
        return {"kb": retrieval.load_kb()}

    def _save_kb(items: list[dict]) -> None:
        p = retrieval.KB_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"kb": items}, f, ensure_ascii=False, indent=2)

    @app.post("/api/kb", dependencies=[Depends(require_admin)])
    def add_kb(body: KbIn):
        items = retrieval.load_kb()
        if any(e["id"] == body.id for e in items):
            raise HTTPException(400, "kb id already exists")
        items.append({"id": body.id, "text": body.text, "title": body.title, "answer": body.answer, "tags": body.tags})
        _save_kb(items)
        return {"kb": items}

    @app.put("/api/kb/{kb_id}", dependencies=[Depends(require_admin)])
    def update_kb(kb_id: str, body: KbIn):
        items = retrieval.load_kb()
        for i, e in enumerate(items):
            if e["id"] == kb_id:
                items[i] = {"id": kb_id, "text": body.text, "title": body.title, "answer": body.answer, "tags": body.tags}
                _save_kb(items)
                return {"kb": items}
        raise HTTPException(404, "kb not found")

    @app.delete("/api/kb/{kb_id}", dependencies=[Depends(require_admin)])
    def delete_kb(kb_id: str):
        items = retrieval.load_kb()
        new = [e for e in items if e["id"] != kb_id]
        if len(new) == len(items):
            raise HTTPException(404, "kb not found")
        _save_kb(new)
        return {"kb": new}

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

    @app.post("/v1/gateway/heartbeat")
    def gateway_heartbeat(body: HeartbeatIn):
        app.state.gateway_channels[body.gateway] = {
            "status": body.status,
            "last_seen": time.time(),
            "detail": body.detail or {},
        }
        return {"ok": True}

    @app.get("/api/admin/sessions", dependencies=[Depends(require_admin)])
    def admin_sessions(status: str | None = None, limit: int = 50, offset: int = 0):
        return store.admin_sessions(limit=limit, offset=offset, status=status)

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

    @app.get("/api/debug/conn")
    def debug_conn():
        return {
            "visitor_sessions": list(hub.visitor_conns.keys()),
            "visitor_total": sum(len(v) for v in hub.visitor_conns.values()),
            "operator_total": len(hub.operator_conns),
        }

    @app.websocket("/ws/operator")
    async def ws_operator(ws: WebSocket):
        await hub.connect_operator(ws)

    @app.websocket("/ws/{sid}")
    async def ws(sid: str, ws: WebSocket):
        await hub.connect(sid, ws)

    # ---- 单端口模式：可选托管前端构建产物（CS_AGENT_STATIC_DIR=frontend/dist）----
    static_dir = os.environ.get("CS_AGENT_STATIC_DIR", "").strip()
    if static_dir and os.path.isdir(static_dir):
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        index_html = os.path.join(static_dir, "index.html")
        assets_dir = os.path.join(static_dir, "assets")
        if os.path.isdir(assets_dir):
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str):
            if full_path.startswith(("api/", "v1/", "ws/", "docs", "openapi.json", "healthz")):
                raise HTTPException(404, "not found")
            candidate = os.path.join(static_dir, full_path)
            if full_path and os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(index_html)

    return app


app = create_app()
