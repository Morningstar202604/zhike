"""URL 抓取的安全护栏（SSRF）。

模型会用 `read_webpage` 读用户给的链接。这是把「用户可控字符串」变成
「服务器发起请求」的经典 SSRF 入口：如果不拦，`http://127.0.0.1:8010/api/config`
或云厂商元数据地址 `http://169.254.169.254/` 都会被读回来送给模型。

规则（默认拒绝，显式放行才通）：
1. 只允许 http/https；
2. 解析出的**每一个** IP 都必须是公网地址（不用「第一个」判断，防 DNS 轮询绕过）；
3. 连 IP 重定向都要重新校验（最多 3 跳）；
4. 响应体有硬上限，避免被超大页面拖死。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}
MAX_REDIRECTS = 3
MAX_BYTES = 2_000_000


class UnsafeUrl(ValueError):
    pass


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def resolve_public(host: str, port: int = 0) -> list[str]:
    """解析主机名，返回全部公网 IP；任何一个非公网就整体拒绝。"""
    try:
        infos = socket.getaddrinfo(host, port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeUrl(f"域名解析失败：{host}") from e
    if not infos:
        raise UnsafeUrl(f"域名没有可用地址：{host}")
    out: list[str] = []
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as e:
            raise UnsafeUrl(f"无法识别的地址：{raw}") from e
        if not _is_public(ip):
            raise UnsafeUrl(f"拒绝访问内网地址：{ip}")
        out.append(raw)
    return out


def check_url(url: str) -> str:
    """校验 URL 可安全抓取，返回规范化后的 URL。"""
    url = (url or "").strip()
    if not url:
        raise UnsafeUrl("URL 为空")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeUrl(f"只支持 http/https，收到：{parsed.scheme or '(空)'}")
    host = parsed.hostname
    if not host:
        raise UnsafeUrl("URL 缺少主机名")
    if is_metadata_host(host):
        raise UnsafeUrl("拒绝访问云元数据地址")
    resolve_public(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    return url


_METADATA_HOSTS = {
    "169.254.169.254",          # AWS / GCP / 阿里云 / Azure 通用元数据
    "metadata.google.internal",
    "metadata",
    "100.100.100.200",          # 阿里云
    "fd00:ec2::254",            # AWS IPv6 元数据
}


def is_metadata_host(host: str) -> bool:
    """只判定云元数据地址（内网端点放行时仍必须拒绝它）。"""
    return (host or "").strip().lower() in _METADATA_HOSTS
