"""LLM API Key 落库加密（Fernet 对称加密）。

Key 来源优先级：
1. 环境变量 ZHIKE_SECRET_KEY（32 字节 urlsafe base64，生产必须设）
2. 否则从 DB 路径 + 机器 host 派生（开发便利，不跨机迁移）

向后兼容：DB 里旧明文 key 不是 Fernet token（不以 'gAAAAA' 开头）时，直接当明文用。
"""
from __future__ import annotations

import os
import socket
import hashlib
import base64

_FERNET = None
_INITIALIZED = False


def _get_fernet():
    global _FERNET, _INITIALIZED
    if _INITIALIZED:
        return _FERNET
    _INITIALIZED = True
    try:
        from cryptography.fernet import Fernet  # type: ignore
    except ImportError:
        _FERNET = None
        return None

    key_material = os.environ.get("ZHIKE_SECRET_KEY", "").strip()
    if not key_material:
        # 开发兜底：从 DB 路径 + hostname 派生（不跨机）
        seed = f"zhike::{socket.gethostname()}::{os.environ.get('CS_AGENT_DB', 'default')}".encode()
        key_material = base64.urlsafe_b64encode(hashlib.sha256(seed).digest()).decode()
    else:
        # 用户给的 key 可能不是 32 字节，sha256 派生到合法长度
        key_material = base64.urlsafe_b64encode(hashlib.sha256(key_material.encode()).digest()).decode()
    try:
        _FERNET = Fernet(key_material.encode())
    except Exception:
        _FERNET = None
    return _FERNET


def encrypt_key(plain: str) -> str:
    """明文 key → 加密 token；cryptography 不可用或空串时原样返回。"""
    if not plain:
        return plain
    f = _get_fernet()
    if f is None:
        return plain
    try:
        return f.encrypt(plain.encode()).decode()
    except Exception:
        return plain


def decrypt_key(stored: str) -> str:
    """加密 token → 明文；非 Fernet token（旧明文）直接返回。"""
    if not stored:
        return stored
    f = _get_fernet()
    # Fernet token 固定以 gAAAAA 开头（base64 of version byte 0x80）
    if f is None or not stored.startswith("gAAAAA"):
        return stored
    try:
        return f.decrypt(stored.encode()).decode()
    except Exception:
        # key 变了（换机器/换 secret）→ 解不开，按明文返回让上层报错而非崩
        return stored
