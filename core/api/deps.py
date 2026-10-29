"""API 层公共依赖：鉴权、限流、入参护栏。

从 core/main.py 抽出，让路由模块（core/api/*）与主应用共享同一套鉴权语义，
避免"每个路由自己写一遍 token 校验"这种典型的安全漏洞来源。
"""
from __future__ import annotations

import os
import re

from fastapi import Header, HTTPException

from core import security, store

# ---- 入参体积护栏（模块级常量，便于配置与测试覆盖）----
MAX_IMAGE_B64_CHARS = int(os.environ.get("MAX_IMAGE_B64_CHARS", str(8_000_000)))
MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", "20000"))
MAX_KB_IMPORT_ITEMS = int(os.environ.get("MAX_KB_IMPORT_ITEMS", "2000"))

# 图片 data URL 只允许这些前缀：拒绝 data:text/html 之类的 XSS 载荷
_ALLOWED_IMAGE_PREFIXES = ("data:image/png", "data:image/jpeg", "data:image/jpg",
                           "data:image/webp", "data:image/gif", "data:image/bmp")


def check_payload(content: str, image: str | None = None) -> None:
    """统一入参护栏：超长文本/超大图片/非图片 data URL 一律 413。"""
    if content and len(content) > MAX_TEXT_CHARS:
        raise HTTPException(413, f"文本过长（上限 {MAX_TEXT_CHARS} 字符）")
    if not image:
        return
    if len(image) > MAX_IMAGE_B64_CHARS:
        raise HTTPException(413, "图片过大，请压缩后再发")
    if image.startswith("data:") and not image.startswith(_ALLOWED_IMAGE_PREFIXES):
        raise HTTPException(400, "只支持图片类型的 data URL")


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


def require_session_secret(sid: str,
                           x_session_secret: str | None = Header(default=None, alias="X-Session-Secret")):
    """web 访客会话归属校验：sid + secret 双因子。渠道会话（QQ 等，由 CHANNEL_TOKEN 保护）不经过此依赖。"""
    if not store.check_session_secret(sid, x_session_secret or ""):
        raise HTTPException(401, "会话无效或归属校验失败")


def require_admin_or_session(
    sid: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    x_session_secret: str | None = Header(default=None, alias="X-Session-Secret"),
):
    """只读会话内容的双重授权：管理员令牌或访客自己的 secret。"""
    token = security.admin_token()
    if token and security.constant_time_equals(x_admin_token, token):
        return
    if not store.check_session_secret(sid, x_session_secret or ""):
        raise HTTPException(401, "会话无效或需要管理员令牌")


_ID_SAFE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def require_id(value: str, field: str = "id") -> str:
    """自由文本 ID 的形状校验（工具 id / 知识库 id 等），防止被拼进 SQL/路径。"""
    v = (value or "").strip()
    if not _ID_SAFE.match(v):
        raise HTTPException(400, f"{field} 只能包含字母、数字、下划线、点和短横线（≤64 字符）")
    return v
