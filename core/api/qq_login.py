"""QQ 扫码登录代理：把 NapCat WebUI 的登录二维码搬进控制台。

## 为什么经核心中转，而不是前端直连 NapCat

- NapCat WebUI（默认 6099）通常只绑本机，浏览器根本够不着（服务器部署时尤其如此）；
- WebUI 的 token 是管理凭据，**绝不能下发到浏览器**——经核心代理后，token 只存在于
  服务端进程内，前端拿到的是二维码 URL 和登录状态，什么都泄漏不了；
- 前端已经用统一的 X-Admin-Token 鉴权体系，扫码入口自然继承同一道门。

## NapCat WebUI 接口（非官方内部接口，NapCat 4.18.x 实测）

鉴权是确定性的：``hash = SHA256(token + ".napcat")``，POST /api/auth/login 换
Credential，后续请求 ``Authorization: Bearer <Credential>``；凭证过期（401/403）
自动重登一次再重放。业务错误常用 HTTP 200 包 ``{"code": 非0, "message"}`` 返回，
所以除了状态码还要看 body 的 code。

端点：
- POST /api/QQLogin/CheckLoginStatus  → data: { loginPhase, isLogin, isOffline, coreReady, ... }
- POST /api/QQLogin/GetQQLoginQrcode  → data: { qrcodeurl }（一段要渲染成二维码的 URL）
- POST /api/QQLogin/RefreshQRcode     → 强制刷新（二维码约 2 分钟过期）

跨版本可能失效：代理层对任何异常都如实返回 error 字符串，前端明说「去 NapCat WebUI 扫」，
绝不假装二维码可用。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time

from fastapi import APIRouter, Depends, HTTPException

from core.api.deps import require_admin
from core.http_pool import get_client

logger = logging.getLogger("cs.events")

router = APIRouter(prefix="/api/channel/qq", tags=["qq-login"])

_NAPCAT_TIMEOUT_S = 6.0
# Credential 缓存：NapCat 的会话凭证有效期较长，进程内缓存足够；过期靠 401 重登兜底
_cred_cache: dict = {"credential": "", "ts": 0.0}
_cred_lock = asyncio.Lock()


def _napcat_conf() -> tuple[str, str]:
    """(base_url, token)。未配置时返回空串，调用方诚实报告「未配置」。"""
    base = (os.environ.get("NAPCAT_WEBUI_URL") or "http://127.0.0.1:6099").rstrip("/")
    token = os.environ.get("NAPCAT_WEBUI_TOKEN") or ""
    return base, token


def napcat_configured() -> bool:
    return bool(_napcat_conf()[1])


async def _login_credential(force: bool = False) -> str:
    """拿 WebUI 会话凭证（SHA256(token+'.napcat') 换 Credential），带进程内缓存。"""
    base, token = _napcat_conf()
    if not token:
        return ""
    if not force and _cred_cache["credential"] and time.time() - _cred_cache["ts"] < 3600:
        return _cred_cache["credential"]
    digest = hashlib.sha256(f"{token}.napcat".encode()).hexdigest()
    client = get_client(base_url=base, timeout=_NAPCAT_TIMEOUT_S)
    resp = await client.post(f"{base}/api/auth/login",
                             json={"hash": digest, "totpCode": ""})
    resp.raise_for_status()
    body = resp.json()
    cred = str((body.get("data") or {}).get("Credential") or "")
    if not cred:
        # WebUI 的业务错误藏在 200 里，把 message 带出去才可诊断
        raise RuntimeError(f"NapCat WebUI 登录失败: {body.get('message') or body.get('code')}")
    _cred_cache["credential"] = cred
    _cred_cache["ts"] = time.time()
    return cred


async def _napcat_post(path: str, payload: dict | None = None) -> dict:
    """带凭证调 WebUI 业务接口；401/403 重登一次再重放。返回 data 字段。"""
    base, token = _napcat_conf()
    if not token:
        raise HTTPException(400, "未配置 NAPCAT_WEBUI_TOKEN，无法代理 NapCat 登录接口")
    client = get_client(base_url=base, timeout=_NAPCAT_TIMEOUT_S)
    cred = await _login_credential()
    for attempt in (0, 1):
        resp = await client.post(f"{base}{path}", json=payload or {},
                                 headers={"Authorization": f"Bearer {cred}"})
        if resp.status_code in (401, 403) and attempt == 0:
            cred = await _login_credential(force=True)
            continue
        resp.raise_for_status()
        body = resp.json()
        code = body.get("code")
        if code not in (None, 0):
            raise RuntimeError(f"NapCat WebUI {path} 返回错误: {body.get('message') or code}")
        return body.get("data") or {}
    raise RuntimeError("NapCat WebUI 凭证重登后仍被拒绝")


@router.get("/qr")
async def qq_login_qr(refresh: str = "", _admin: None = Depends(require_admin)):
    """当前登录二维码（qrcodeurl 是一段 URL，前端渲染成二维码图）。

    `?refresh=1` 强制刷新——二维码约 2 分钟过期，过期后必须换新而不是重试旧的。
    """
    if not napcat_configured():
        return {"configured": False,
                "error": "未配置 NAPCAT_WEBUI_TOKEN（NapCat WebUI 令牌），无法获取二维码"}
    try:
        path = "/api/QQLogin/RefreshQRcode" if refresh in ("1", "true") \
            else "/api/QQLogin/GetQQLoginQrcode"
        data = await _napcat_post(path)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("napcat qr proxy failed: %s", type(e).__name__)
        return {"configured": True, "error": f"拿二维码失败：{type(e).__name__}: {str(e)[:160]}"}
    url = str(data.get("qrcodeurl") or data.get("qrcode") or "")
    if not url:
        return {"configured": True,
                "error": "NapCat 没有返回二维码（可能已登录或 WebUI 版本不兼容）"}
    return {"configured": True, "qrcode_url": url}


@router.get("/login-status")
async def qq_login_status(_admin: None = Depends(require_admin)):
    """NapCat 登录阶段：isLogin / isOffline / coreReady / loginPhase 原样透传。"""
    if not napcat_configured():
        return {"configured": False}
    try:
        data = await _napcat_post("/api/QQLogin/CheckLoginStatus")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("napcat status proxy failed: %s", type(e).__name__)
        return {"configured": True, "reachable": False,
                "error": f"连不上 NapCat WebUI：{type(e).__name__}: {str(e)[:160]}"}
    return {
        "configured": True,
        "reachable": True,
        "is_login": bool(data.get("isLogin")),
        "is_offline": bool(data.get("isOffline")),
        "core_ready": bool(data.get("coreReady")),
        "login_phase": data.get("loginPhase"),
        "uin": str(data.get("uin") or data.get("uin64") or ""),
        "nick": str(data.get("nick") or ""),
        # 原样透传一份：NapCat 版本间字段有差异，前端对未知字段保持宽容
        "raw": data,
    }
