"""NoneBot2 网关入口：OneBot v11（NapCat / Lagrange / LLOneBot）→ 核心服务 /v1/ingest。

启动：python gateway/bot.py（先配置 ONEBOT_WS_URL 与 CORE_BASE_URL 环境变量）。
可选 CHANNEL_TOKEN：与核心服务一致时以 X-Channel-Token 头上报，防伪造消息注入。
"""
import asyncio
import base64
import collections
import logging
import os
import random
import secrets
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

import nonebot
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter, MessageEvent
from nonebot.rule import to_me

from gateway.adapters import OneBot11Adapter, bus, registry
from gateway.adapters.bus import EV_ACTION_DONE, EV_MESSAGE_IN, EV_REPLY_OUT
from gateway.humanize import EmoteLib, GroupThrottle, render, send_delay, split_reply

logger = logging.getLogger("gateway")

# 群友表情包收藏库（第三方表情包学习）：mface 回放 + 梗图本地缓存
_EMOTE_DIR = os.environ.get("GATEWAY_EMOTE_DIR", "C:/Users/YOU/napcat/emotes")
_EMOTE_LIB = EmoteLib(os.path.join(_EMOTE_DIR, "index.json"))
os.makedirs(_EMOTE_DIR, exist_ok=True)
# 梗图下载限流：每小时最多 30 张、并发 2——别把带宽和磁盘搞爆
_EMOTE_DL_SEM = asyncio.Semaphore(2)
_EMOTE_DL_TIMES: list[float] = []


def _env_f(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, "") or default)
        return v if v >= 0 else default
    except ValueError:
        return default

# 适配器实例：整个网关生命周期复用（重连时 bind 新 bot）
adapter = OneBot11Adapter()

# 网关自己的内部 HTTP 端口：core 通过 GATEWAY_ACTION_URL 调 /internal/action。
# 默认必须是 8011（与 core.actions.client 和 compose 一致）；NoneBot 自身默认 8080
# 会与很多本机服务冲突，而且 core 永远连不上（实测 /api/gateway/health 一直离线）。
os.environ.setdefault("PORT", "8011")
# 本地默认只绑回环；compose 显式设 HOST=0.0.0.0
os.environ.setdefault("HOST", "127.0.0.1")

_init_kwargs: dict = {
    "driver": "~fastapi+~websockets",
    "onebot_ws_urls": [os.environ.get("ONEBOT_WS_URL", "ws://127.0.0.1:3001")],
}
# 协议端若启用 access token（NapCat WebUI 可配），网关必须带上，否则握手被 403。
# 用 NoneBot 配置项 ONEBOT_ACCESS_TOKEN 传入，适配器会以 Authorization: Bearer 发送。
if os.environ.get("ONEBOT_ACCESS_TOKEN"):
    _init_kwargs["onebot_access_token"] = os.environ["ONEBOT_ACCESS_TOKEN"]

def _preflight_ws() -> None:
    """启动前探测协议端 WS 是否真的可握手，给出人话提示而不是一屏 traceback。

    为什么不能只看端口：NapCat 未登录时端口已经监听，但 WS 升级请求收不到响应
    （实测：TCP connect 成功、HTTP 101 永不返回），直接启动只会反复抛 InvalidMessage。

    GATEWAY_SKIP_PREFLIGHT=1 时跳过：端到端演练里网关要先于假协议端起进程。
    """
    import socket
    from urllib.parse import urlparse

    if os.environ.get("GATEWAY_SKIP_PREFLIGHT", "").strip().lower() in ("1", "true", "yes"):
        return

    url = os.environ.get("ONEBOT_WS_URL", "ws://127.0.0.1:3001")
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 3001
    crlf = chr(13) + chr(10)

    def probe() -> tuple[bool, str]:
        try:
            sock = socket.create_connection((host, port), timeout=3)
        except OSError as e:
            return False, f"端口不可达（{type(e).__name__}）"
        try:
            request = crlf.join([
                "GET / HTTP/1.1",
                f"Host: {host}:{port}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                # 必须是 16 字节随机数的 base64；写死字符串会让严格实现直接拒绝握手
                f"Sec-WebSocket-Key: {base64.b64encode(secrets.token_bytes(16)).decode()}",
                "Sec-WebSocket-Version: 13",
                "",
                "",
            ]).encode()
            sock.sendall(request)
            sock.settimeout(3)
            head = sock.recv(64)
            if not head:
                return False, "端口可连但无 HTTP 响应（QQ 未登录时的典型表现）"
            status_line = head.split(b"\r\n", 1)[0] if b"\r\n" in head else head.split(b"\n", 1)[0]
            if b" 101 " in status_line:
                return True, ""
            return False, f"WS 升级被拒绝：{status_line[:60]!r}"
        except OSError as e:
            return False, f"握手超时/中断（{type(e).__name__}）"
        finally:
            sock.close()

    ok, reason = probe()
    if ok:
        return
    lines = [
        f"协议端 {url} 不可用：{reason}。",
        "  1) NapCat 容器是否在运行：docker ps | grep napcat",
        "  2) QQ 是否已登录：python scripts/qq_login.py（看到「OneBot WS 已就绪」再启动网关）",
        f"  3) NapCat 网络配置里 WS 服务端是否启用且端口为 {port}",
        "  详见 docs/QQ-LOGIN.md",
    ]
    logger.error(chr(10).join(lines))
    sys.exit(2)


nonebot.init(**_init_kwargs)
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

CORE_BASE_URL = os.environ.get("CORE_BASE_URL", "http://127.0.0.1:8000")
CHANNEL_TOKEN = os.environ.get("CHANNEL_TOKEN", "")
# 单条消息等待核心的最长时间：LLM ReAct + 工具调用可能到 40s 以上，默认放宽到 90s
INGEST_TIMEOUT_S = float(os.environ.get("INGEST_TIMEOUT_S", "90"))
# 同时处理的消息上限：群里一次 @ 全体或刷屏时，避免把核心与模型打爆
MAX_CONCURRENT = max(1, int(os.environ.get("GATEWAY_MAX_CONCURRENT", "8")))
# 同一会话内串行：保证群友连发多条时回复顺序与提问一致
_PER_CHAT_LOCKS: dict[str, asyncio.Lock] = {}
# 已处理 message_id（LRU）：协议端重连会重放最近事件，不去重会重复回复
_SEEN_IDS: "collections.OrderedDict[str, float]" = collections.OrderedDict()
_SEEN_MAX = 2000
_sem = asyncio.Semaphore(MAX_CONCURRENT)

# 群聊防风控节流：随机化最小间隔 + 每小时上限，超限静默丢回复——
# 群里一次 @ 一条条秒回、刷屏式回答，是最容易被检测出自动化的行为。
_GROUP_THROTTLE = GroupThrottle(
    min_gap=_env_f("GATEWAY_GROUP_MIN_GAP", 10.0),
    hourly_cap=int(_env_f("GATEWAY_GROUP_HOURLY_CAP", 120)),
)

matcher = on_message(rule=to_me(), priority=5, block=False)

# ---- 表情包收藏器（AstrBot「梗图学习」对标）：监听全部群消息，只收集绝不回复 ----
_collector_matcher = on_message(priority=1, block=False)

# ---- 群环境感知：氛围缓冲（最近群聊）+ 群名缓存（QQ 群上下文注入）----
_AMBIENT: dict[str, collections.deque] = {}
_AMBIENT_MAX = 12
_GROUP_NAMES: dict[str, str] = {}


_AMBIENT_MAX_GROUPS = max(64, int(os.environ.get("GATEWAY_AMBIENT_MAX_GROUPS", "200")))


def _ambient_push(group: str, line: str) -> None:
    buf = _AMBIENT.setdefault(group, collections.deque(maxlen=_AMBIENT_MAX))
    buf.append(line)
    # 群数量无上限时 dict 会随运行时间无限增长（每个群一条 deque）。
    # 超过上限丢最早插入的群：氛围只是锦上添花，丢历史无副作用。
    while len(_AMBIENT) > _AMBIENT_MAX_GROUPS:
        _AMBIENT.pop(next(iter(_AMBIENT)), None)


def _ambient_snapshot(group: str) -> list[str]:
    buf = _AMBIENT.get(group)
    if not buf:
        return []
    now = time.time()
    return [ln for ln in buf if now - ln.get("ts", 0) < 600][-8:]


async def _group_name(bot, group_id: str) -> str:
    """群名（带缓存）。任何失败返回空串，绝不阻塞回复主链路。"""
    if group_id in _GROUP_NAMES:
        return _GROUP_NAMES[group_id]
    try:
        info = await bot.call_api("get_group_info", group_id=int(group_id) if group_id.isdigit() else group_id)
        name = str((info or {}).get("group_name", "") or "")
        _GROUP_NAMES[group_id] = name
        return name
    except Exception:
        return ""



async def _download_emote(url: str, group: str, summary: str) -> None:
    """下载群友动画表情到本地（限 800KB、每小时 30 张、并发 2）。"""
    now = time.time()
    while _EMOTE_DL_TIMES and now - _EMOTE_DL_TIMES[0] > 3600:
        _EMOTE_DL_TIMES.pop(0)
    if len(_EMOTE_DL_TIMES) >= int(_env_f("GATEWAY_EMOTE_HOURLY", 30)):
        return
    from core import url_guard

    try:
        safe = url_guard.check_url(url)
    except Exception:
        return
    try:
        async with _EMOTE_DL_SEM:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                resp = await client.get(safe)
                if resp.status_code != 200 or len(resp.content) > 800 * 1024:
                    return
                _EMOTE_DL_TIMES.append(time.time())
            ext = ".gif" if resp.content[:3] == b"GIF" else \
                  ".png" if resp.content[:4] == b"\x89PNG" else ".jpg"
            fname = f"e{int(time.time()*1000)}{ext}"
            fpath = os.path.join(_EMOTE_DIR, fname)
            with open(fpath, "wb") as f:
                f.write(resp.content)
        if _EMOTE_LIB.add("image", summary, fpath, group):
            _EMOTE_LIB.save()
    except Exception:  # 收藏失败绝不能影响正常收发
        pass


@_collector_matcher.handle()
async def _collect(event: MessageEvent):
    """扫群消息：氛围缓冲 + 第三方表情收藏（mface 回放 / 梗图下载缓存）。绝不回复。"""
    try:
        group = str(getattr(event, "group_id", "") or "")
        nick = (event.sender.nickname if event.sender else "") or ""
        text = event.get_plaintext() or ""
        if str(event.user_id) == str(event.self_id):
            return  # 机器人自己的回显不入氛围
        if group and (text.strip() or "CQ:image" in str(event.message)):
            _ambient_push(group, {"nick": nick[:12], "text": text[:60],
                                  "has_img": "CQ:image" in str(event.message), "ts": time.time()})
        for seg in event.message:
            if seg.type == "mface":
                summary = str(seg.data.get("summary", "") or "")
                if _EMOTE_LIB.add("mface", summary, str(seg), group):
                    _EMOTE_LIB.save()
            elif seg.type == "image":
                summary = str(seg.data.get("summary", "") or "")
                url = str(seg.data.get("url", "") or "")
                # 群里绝大多数梗图 summary 都是「[动画表情]」，直接拿来当关键词
                # 会让「按关键词找表情」永远命中同一批空标签。用「群号+序号」兜底，
                # 同时把 summary 清空，避免库里 300 条全是同一个词。
                if url and ("动画表情" in summary or "表情" in summary):
                    if not summary.strip("[]") or summary.strip("[]") == "动画表情":
                        summary = ""
                    asyncio.create_task(_download_emote(url, group, summary))
    except Exception:  # aqg: 收集器顶层兜底
        pass


# ---- QQ 侧指令（AstrBot 对标）：/开头直接在网关处理，不进核心管线 ----
# 设计原则：只做「无状态 + 只读 + 重置」三类；一切业务判断仍在 core。
_CMD_START = time.time()
_CMD_STATS = {"commands": 0, "handled": 0}
HELP_TEXT = (
    "我能做的事（@我或私聊）：\n"
    "/帮助 — 显示本菜单\n"
    "/状态 — 我的运行状态\n"
    "/重置 — 开启全新对话（清上下文，长期记忆保留）\n"
    "平时直接说话就行：设提醒、记事、查资料、看群消息、发图、读文件都能办。"
)


def _uptime_text() -> str:
    secs = int(time.time() - _CMD_START)
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h}小时{m}分" if h else f"{m}分钟"


async def _handle_command(msg, text: str) -> bool:
    """QQ 侧指令处理。返回 True 表示已作为指令消费（不再进核心管线）。"""
    cmd = text.split()[0].lower()
    alias = {
        "/帮助": "help", "/help": "help", "/菜单": "help", "/指令": "help",
        "/状态": "status", "/status": "status",
        "/重置": "reset", "/reset": "reset", "/新对话": "reset", "/清空": "reset",
    }
    action = alias.get(cmd)
    if action is None:
        return False
    _CMD_STATS["commands"] += 1
    try:
        if action == "help":
            reply = HELP_TEXT
        elif action == "status":
            bot = _pick_bot()
            reply = (
                f"在线：{bot.self_id if bot else '未知'}\n"
                f"跑了 {_uptime_text()}\n"
                f"已处理 {_CMD_STATS['handled']} 条消息、{_CMD_STATS['commands']} 条指令\n"
                "发 /帮助 看我能干啥"
            )
        elif action == "reset":
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{CORE_BASE_URL}/v1/channel/reset",
                    json={
                        "channel_type": msg.channel,
                        "channel_user_id": msg.sender_id,
                        "group_id": msg.chat_id if msg.is_group else None,
                    },
                    headers=_channel_headers(),
                )
                resp.raise_for_status()
            reply = "行，翻篇了。刚才聊的我不提了，你记的事还留着。"
        else:
            return False
    except Exception as e:  # 指令失败给明确反馈，不吞异常
        logger.warning("command %s failed: %s: %s", cmd, type(e).__name__, str(e)[:120])
        reply = "指令执行失败，请稍后再试～"
    await adapter.send(msg, reply)
    return True


def _dedup(message_id: str | None) -> bool:
    """True = 首次见到，可以处理；False = 重复投递。"""
    if not message_id:
        return True
    now = time.time()
    if message_id in _SEEN_IDS:
        return False
    _SEEN_IDS[message_id] = now
    while len(_SEEN_IDS) > _SEEN_MAX:
        _SEEN_IDS.popitem(last=False)
    return True


_PER_CHAT_LOCKS_MAX = max(64, int(os.environ.get("GATEWAY_CHAT_LOCKS_MAX", "500")))


def _chat_lock(key: str) -> asyncio.Lock:
    lock = _PER_CHAT_LOCKS.get(key)
    if lock is None:
        # 空闲锁可以安全丢弃重建：有任务持有的锁 locked() 为 True，不会被回收。
        while len(_PER_CHAT_LOCKS) >= _PER_CHAT_LOCKS_MAX:
            victim = next((k for k, l in _PER_CHAT_LOCKS.items() if not l.locked()), None)
            if victim is None:
                break
            _PER_CHAT_LOCKS.pop(victim, None)
        lock = asyncio.Lock()
        _PER_CHAT_LOCKS[key] = lock
    return lock


def _channel_headers() -> dict[str, str]:
    return {"X-Channel-Token": CHANNEL_TOKEN} if CHANNEL_TOKEN else {}


def _pick_bot():
    """取当前在线的 bot。多账号时取第一个；无 bot 返回 None。"""
    bots = list(nonebot.get_bots().values())
    return bots[0] if bots else None


def _register_action_endpoint() -> None:
    """向 NoneBot 的 FastAPI driver 挂内部动作端点。

    只监听内网/回环（compose 里 gateway 不发布端口），靠 X-Channel-Token 鉴权。
    网关只做协议转换：不做权限判断、不做业务逻辑，那些都在 core 侧完成。
    """
    import hmac

    from fastapi import Request
    from fastapi.responses import JSONResponse

    app = driver.server_app

    def _authorized(req: Request) -> bool:
        if not CHANNEL_TOKEN:
            # 未配置令牌视为未启用（与 core 的 503 语义一致）
            return False
        got = req.headers.get("x-channel-token", "")
        return hmac.compare_digest(got, CHANNEL_TOKEN)

    @app.post("/internal/action")
    async def _action(req: Request):
        if not _authorized(req):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            body = await req.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

        action = (body or {}).get("action")
        params = (body or {}).get("params") or {}
        if not isinstance(action, str) or not action:
            return JSONResponse({"ok": False, "error": "missing action"}, status_code=400)
        if not isinstance(params, dict):
            return JSONResponse({"ok": False, "error": "params must be object"}, status_code=400)

        bot = _pick_bot()
        if bot is None:
            return JSONResponse(
                {"ok": False, "error": "协议端未连接（机器人未登录）"}, status_code=503
            )
        try:
            data = await bot.call_api(action, **params)
        except Exception as e:  # 协议端拒绝/超时：转成结构化错误，不抛 traceback
            logger.warning("action %s failed: %s: %s", action, type(e).__name__, str(e)[:150])
            return JSONResponse(
                {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}, status_code=502
            )
        return {"ok": True, "data": data}

    @app.get("/internal/status")
    async def _status(req: Request):
        if not _authorized(req):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        bot = _pick_bot()
        return {
            "connected": bot is not None,
            "self_id": str(bot.self_id) if bot is not None else "",
            "bots": len(nonebot.get_bots()),
        }

    logger.info("internal action endpoint registered at /internal/action")


_register_action_endpoint()


async def _fetch_image_b64(url: str) -> str:
    """拉取 QQ 图片转 base64（8MB 上限）；任何失败静默降级为纯文本，不阻塞回复主链路。

    SSRF 防护：QQ 图片 URL 来自群成员，不能直接替他们请求内网/云元数据地址。
    url_guard 校验 + 关闭跨域重定向 + 流式读取硬上限（不先整包进内存）。
    """
    from core import url_guard

    try:
        safe = url_guard.check_url(url)
    except Exception:
        return ""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            async with client.stream("GET", safe) as resp:
                resp.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > 8 * 1024 * 1024:
                        break
                    chunks.append(chunk)
        return base64.b64encode(b"".join(chunks)[: 8 * 1024 * 1024]).decode()
    except Exception:  # aqg: top-level boundary
        return ""


_heartbeat_task: asyncio.Task | None = None
HEARTBEAT_INTERVAL_S = int(os.environ.get("HEARTBEAT_INTERVAL_S", "30"))


async def _heartbeat_loop():
    """定时上报心跳。连续失败会记日志（不刷屏），且该任务可被 shutdown 取消。"""
    consecutive_failures = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{CORE_BASE_URL}/v1/gateway/heartbeat",
                    json={
                        "gateway": "onebot_v11",
                        "status": "online",
                        "detail": {
                            "adapter": "nonebot2+onebot_v11",
                            "self_id": _pick_bot().self_id if _pick_bot() else "",
                            "capabilities": sorted(adapter.capabilities()),
                        },
                    },
                    headers=_channel_headers(),
                )
                resp.raise_for_status()
            if consecutive_failures:
                logger.info("heartbeat recovered after %d failures", consecutive_failures)
                consecutive_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as e:  # 心跳失败不能拖垮网关，但也不能完全静默
            consecutive_failures += 1
            # 只在前几次与每 10 次时输出，避免核心长时间不可用时刷爆日志
            if consecutive_failures <= 3 or consecutive_failures % 10 == 0:
                logger.warning("heartbeat failed (%d): %s: %s",
                               consecutive_failures, type(e).__name__, str(e)[:120])
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
        except asyncio.CancelledError:
            raise


@driver.on_startup
async def _start_heartbeat():
    global _heartbeat_task
    _heartbeat_task = asyncio.create_task(_heartbeat_loop())
    # 保存引用可避免任务被 GC 回收；同时让 shutdown 能干净地停掉它
    logger.info("heartbeat started, interval=%ss", HEARTBEAT_INTERVAL_S)


@driver.on_shutdown
async def _stop_heartbeat():
    global _heartbeat_task
    if _heartbeat_task and not _heartbeat_task.done():
        _heartbeat_task.cancel()
        try:
            await _heartbeat_task
        except asyncio.CancelledError:
            pass
    _heartbeat_task = None


def _event_to_raw(event: MessageEvent) -> dict:
    """NoneBot 事件 → OneBot 原始 dict（适配器只吃原始 dict，便于脱离框架测试）。"""
    segs = [{"type": s.type, "data": dict(s.data)} for s in event.message]
    return {
        "post_type": "message",
        "self_id": str(event.self_id),
        "user_id": event.user_id,
        "message_type": event.message_type,
        "group_id": getattr(event, "group_id", None),
        "message": segs,
        "sender": {"nickname": (event.sender.nickname if event.sender else "") or ""},
        "message_id": event.message_id,
        # 适配器已剥离命中的 @ 段，群聊判定依赖事件级 to_me
        "to_me": bool(getattr(event, "to_me", False)),
    }


@driver.on_startup
async def _bind_adapter():
    """启动时把在线 bot 绑到适配器上，并把适配器登记进注册表。"""
    bot = _pick_bot()
    if bot is not None:
        adapter.bind(bot)
    try:
        registry.register(adapter)
    except ValueError:
        pass  # 已注册（重连场景）
    logger.info("adapter bound: %s", adapter.name)


async def _post_ingest(payload: dict) -> str:
    """调用核心 /v1/ingest，网络类错误重试一次（指数退避）。

    只重试连接/超时/5xx：4xx 是业务拒绝（例如限流、内容校验），重试没有意义。
    """
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=INGEST_TIMEOUT_S) as client:
                resp = await client.post(
                    f"{CORE_BASE_URL}/v1/ingest", json=payload, headers=_channel_headers()
                )
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"core {resp.status_code}", request=resp.request, response=resp)
                if resp.status_code == 429:
                    # 核心限流：静默退避。回错误话术也占一次发送频率，不如闭嘴。
                    return {"mode": "rate_limited", "text": ""}
                resp.raise_for_status()
                return resp.json()["reply"]
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
                httpx.HTTPStatusError) as e:
            last_exc = e
            if attempt == 0:
                await asyncio.sleep(0.6)
                continue
            raise
    raise last_exc or RuntimeError("ingest failed")


@matcher.handle()
async def handle(event: MessageEvent):
    # 单一事实源：统一走适配器接口（内部仍是 mapping 纯函数），不在网关层维护第二套解析
    bot = _pick_bot()
    if bot is not None:
        adapter.bind(bot)

    msg = adapter.parse(_event_to_raw(event))
    if msg is None:
        return

    # QQ 侧指令：/开头且命中已知指令时在网关本地消费，不进核心管线
    if msg.text.startswith("/"):
        if await _handle_command(msg, msg.text.strip()):
            return

    if not _dedup(msg.message_id):
        logger.info("duplicate message skipped: %s", msg.message_id)
        return

    await bus.publish(EV_MESSAGE_IN, {
        "channel": msg.channel, "chat_id": msg.chat_id,
        "sender": msg.sender_id, "text": msg.text[:100],
    })

    payload = {
        "channel_type": msg.channel,
        "channel_user_id": msg.sender_id,
        "group_id": msg.chat_id if msg.is_group else None,
        "text": msg.text,
        "display_name": msg.display_name,
        "message_id": msg.message_id,
    }
    # 群环境感知：@ 之前群里在聊什么（氛围）+ 群名（场景）
    if msg.is_group:
        ambient_lines = [
            f"{ln.get('nick','?')}: {ln.get('text','')}" + ("［表情］" if ln.get("has_img") else "")
            for ln in _ambient_snapshot(msg.chat_id)
        ]
        payload["ambient"] = ambient_lines
        payload["group_name"] = await _group_name(bot, msg.chat_id) if bot is not None else ""
    if msg.images:
        image_b64 = await _fetch_image_b64(msg.images[0])
        if image_b64:
            payload["image_b64"] = image_b64

    # 引用回复：把用户这条消息的 message_id 交给适配器，first 段带 [CQ:reply]。
    # 群里消息刷得快，不带引用就不知道在回哪一句（用户明确要求）。
    quote_id = msg.message_id if msg.is_group else None
    started = time.time()
    reply: dict | None = None
    # 同一会话串行 + 全局并发上限：群里刷屏时既不乱序也不打爆核心。
    # 全局信号量只罩住「回源 ingest」这一段：拟人延迟最长 20 多秒，
    # 若连发送阶段一起罩住，8 个并发槽会被 sleep 耗尽，其他会话全部排队（已复现）。
    async with _chat_lock(f"{msg.channel}:{msg.chat_id}"):
        try:
            async with _sem:
                reply = await _post_ingest(payload)
            reply_text = (reply or {}).get("text") or ""
            await bus.publish(EV_ACTION_DONE, {
                "action": "ingest", "duration_ms": int((time.time() - started) * 1000), "ok": True,
            })
        except Exception as e:  # 核心不可达时给用户明确反馈，而不是沉默
            logger.warning("ingest failed: %s: %s", type(e).__name__, str(e)[:120])
            reply_text = ""  # 网关自己出问题时不冒泡给用户，只记日志
            await bus.publish(EV_ACTION_DONE, {
                "action": "ingest", "duration_ms": int((time.time() - started) * 1000),
                "ok": False, "error": f"{type(e).__name__}",
            })

        # 白名单外的群 / 核心限流 / 没接通模型 / 空回复：静默，不发消息。
        # no_model 是「LLM 未配置或不可用」——此时宁可不说话，也不要用规则话术
        # 顶着"秘书"身份乱回（用户明确要求过：没连上模型就别回复）。
        mode = (reply or {}).get("mode") or ""
        if mode in ("not_allowed", "rate_limited", "no_model") or not reply_text.strip():
            if mode == "no_model":
                logger.warning("model unavailable, reply suppressed (chat=%s)", msg.chat_id)
            return

        # 渲染管线：markdown 净化 → 小黄脸/图片标记 → 表情包收藏 → CQ 码
        out_text = _EMOTE_LIB.render(render(reply_text))
        if not out_text.strip():
            return

        # 拟人化节奏：群聊频控（首条前检查）+ 打字延迟
        if msg.is_group:
            wait = _GROUP_THROTTLE.wait_seconds(msg.chat_id)
            if wait < 0:
                logger.warning("group %s hit hourly cap (%s/h), reply dropped",
                               msg.chat_id, _GROUP_THROTTLE.cap)
                _CMD_STATS["handled"] += 1
                await bus.publish(EV_REPLY_OUT, {
                    "channel": msg.channel, "chat_id": msg.chat_id, "ok": False,
                    "skipped": "hourly_cap",
                })
                return
            if wait > 0:
                await asyncio.sleep(wait)

        # 长回复拆成几条连发——没有真人一次性甩 300 字，真人都是一条条蹦的
        segments = split_reply(out_text,
                               max_chunk=int(_env_f("GATEWAY_SPLIT_CHARS", 90)),
                               max_parts=int(_env_f("GATEWAY_SPLIT_MAX", 3)))
        result = None
        for i, seg in enumerate(segments):
            if i:
                # 同一条回复的连发间隔短于跨回复间隔（2.5~4.5s），像真人刷屏式补充
                await asyncio.sleep(_env_f("GATEWAY_SPLIT_GAP", 2.5) + random.random() * 2.0)
            await asyncio.sleep(send_delay(len(seg), msg.is_group) * (1.0 if i == 0 else 0.6))
            result = await adapter.send(msg, seg, quote=quote_id)
            if msg.is_group:
                _GROUP_THROTTLE.record(msg.chat_id)
            if not result.ok:
                logger.warning("reply send failed (part %d/%d): %s",
                               i + 1, len(segments), result.error)
                await asyncio.sleep(0.5)
                result = await adapter.send(msg, seg, quote=quote_id)
                if not result.ok:
                    break
    _CMD_STATS["handled"] += 1
    await bus.publish(EV_REPLY_OUT, {
        "channel": msg.channel, "chat_id": msg.chat_id, "ok": result.ok,
    })


if __name__ == "__main__":
    _preflight_ws()
    nonebot.run()
