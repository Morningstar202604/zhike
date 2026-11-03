"""版本信息的唯一事实源。

版本号只写在这里和仓库根的 ``VERSION`` 文件，``pyproject.toml`` 保持同步。
不要在其它模块里硬编码版本字符串——``/healthz``、``/api/meta``、前端关于页
都从这里读，避免出现"包版本 1.2.0、接口报 1.1.0"这类不一致。

版本规则（SemVer 2.0.0，见 CHANGELOG.md 与 docs/RELEASING.md）：

- MAJOR：不兼容的对外变更（配置项删除/改名、API 破坏性变更、数据库不可自动迁移）
- MINOR：向后兼容的功能新增
- PATCH：向后兼容的缺陷修复、性能优化、文档与依赖补丁
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["VERSION", "version_info", "version_tuple"]

#: 语义化版本字符串，例如 ``1.2.0``
VERSION = "1.4.1"


def version_tuple(version: str = VERSION) -> tuple[int, int, int]:
    """把 ``1.2.0`` 拆成 ``(1, 2, 0)``；预发布后缀（``1.2.0-rc.1``）取数字部分。"""
    core = version.split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    nums = []
    for part in parts[:3]:
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)  # type: ignore[return-value]


def version_info() -> dict[str, object]:
    """供健康检查与元信息接口使用的结构化版本信息。"""
    major, minor, patch = version_tuple()
    return {
        "version": VERSION,
        "major": major,
        "minor": minor,
        "patch": patch,
        "channel": "stable",
    }


def _read_version_file() -> str | None:
    """读取仓库根 ``VERSION``，用于开发期自检（打包后可能不存在）。"""
    try:
        path = Path(__file__).resolve().parent.parent / "VERSION"
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _self_check() -> None:
    """导入期轻量校验：``VERSION`` 与 ``VERSION`` 文件不一致时只在调试下提示。

    不抛异常——生产环境缺文件不应导致服务起不来；不一致由 CI 的
    ``scripts/check_version.py`` 拦截。
    """
    import os

    if os.environ.get("ZHIKE_VERSION_STRICT", "").strip().lower() not in ("1", "true", "yes", "on"):
        return
    file_version = _read_version_file()
    if file_version and file_version != VERSION:
        raise RuntimeError(
            f"版本不一致：core/version.py={VERSION!r}，VERSION 文件={file_version!r}。"
            "请运行 scripts/check_version.py 或同步两处。"
        )


_self_check()
