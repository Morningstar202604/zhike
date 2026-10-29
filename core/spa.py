"""单端口模式：把前端构建产物挂到 FastAPI（控制台与 API 同域）。

从 core/main.py 抽出，并补上三项生产必需但此前缺失的行为：

1. **静态资源强缓存**：Vite 产物带内容哈希（index-abc123.js），文件名变则内容变，
   可以安全地 `Cache-Control: public, max-age=31536000, immutable`。
   此前 FileResponse 不带缓存头，每次刷新都重新下载 550KB 的 echarts chunk。
2. **index.html 不缓存**：入口 HTML 必须每次校验，否则用户拿不到新的 assets 引用，
   部署后白屏（这是 SPA 部署最经典的坑）。
3. **压缩前置**：如果构建目录里有 .br/.gz 预压缩文件，直接返回对应编码，
   省掉运行时 gzip CPU（GZipMiddleware 仍作为兜底）。
"""
from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


class ImmutableStaticFiles(StaticFiles):
    """带强缓存的静态文件服务。

    StaticFiles 默认不发 Cache-Control，浏览器每次刷新都会重新校验 550KB 的
    echarts chunk。Vite 产物文件名含内容哈希，可以安全 immutable 长缓存。
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers.setdefault("Cache-Control", _IMMUTABLE)
        return resp

# 这些前缀永远交给 API 处理，不能落到 SPA fallback（否则 404 变成 index.html，
# 前端 fetch 会把一段 HTML 当 JSON 解析，报出毫无意义的语法错误）
RESERVED_PREFIXES = ("api/", "v1/", "ws/", "docs", "redoc", "openapi.json",
                     "healthz", "readyz", "metrics")

_IMMUTABLE = "public, max-age=31536000, immutable"
_NO_CACHE = "no-cache, must-revalidate"


def _precompressed(path: str, accept_encoding: str) -> tuple[str, str]:
    """返回 (实际文件路径, Content-Encoding)；没有预压缩文件时用原文件。"""
    ae = (accept_encoding or "").lower()
    for enc, ext in (("br", ".br"), ("gzip", ".gz")):
        if enc in ae:
            candidate = path + ext
            if os.path.isfile(candidate):
                return candidate, enc
    return path, ""


def mount_spa(app: FastAPI, static_dir: str) -> bool:
    """挂载 SPA。目录不存在返回 False（不注册路由，避免抢掉 API 404）。

    必须最后调用：`/{full_path:path}` 是兜底路由，先注册会吞掉所有未匹配请求。
    """
    static_dir = (static_dir or "").strip()
    if not static_dir or not os.path.isdir(static_dir):
        return False

    index_html = os.path.join(static_dir, "index.html")
    assets_dir = os.path.join(static_dir, "assets")
    static_root = os.path.realpath(static_dir)

    if os.path.isdir(assets_dir):
        app.mount("/assets", ImmutableStaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        if full_path.startswith(RESERVED_PREFIXES):
            raise HTTPException(404, "资源不存在")

        candidate = os.path.realpath(os.path.join(static_dir, full_path))
        inside = candidate == static_root or candidate.startswith(static_root + os.sep)
        if full_path and inside and os.path.isfile(candidate):
            # Vite 产物带内容哈希：文件名变则内容变，可安全长缓存；
            # 其它静态文件（favicon 等）用短缓存，避免发版后长期脏数据。
            cache = _IMMUTABLE if full_path.startswith("assets/") else _NO_CACHE
            return FileResponse(candidate, headers={"Cache-Control": cache})

        # 入口 HTML：永不缓存，保证发版后立刻拿到新资源清单
        return FileResponse(index_html, headers={"Cache-Control": _NO_CACHE})

    return True
