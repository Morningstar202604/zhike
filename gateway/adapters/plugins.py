"""插件清单：声明式扩展，加载时校验权限边界。

插件只能「声明需要什么」，实际授权由策略决定。
声明之外的调用一律拒绝——这是防止第三方插件越权的关键。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class PluginError(Exception):
    pass


@dataclass
class PluginManifest:
    name: str
    version: str = "0.1.0"
    entry: str = ""
    description: str = ""
    provides: list[str] = field(default_factory=list)     # channel:wecom 等
    requires: list[str] = field(default_factory=list)     # 依赖的其他插件能力
    permissions: dict = field(default_factory=dict)       # actions / fs / shell
    trust: str = "in-process"

    @classmethod
    def from_dict(cls, data: dict) -> "PluginManifest":
        if not isinstance(data, dict):
            raise PluginError("清单必须是 JSON 对象")
        name = str(data.get("name") or "").strip()
        if not name:
            raise PluginError("清单缺少 name")
        perms = data.get("permissions") or {}
        if not isinstance(perms, dict):
            raise PluginError("permissions 必须是对象")
        trust = str(data.get("trust") or "in-process")
        if trust not in ("in-process", "sandboxed"):
            raise PluginError(f"未知 trust 级别：{trust}")
        return cls(
            name=name,
            version=str(data.get("version") or "0.1.0"),
            entry=str(data.get("entry") or ""),
            description=str(data.get("description") or ""),
            provides=[str(x) for x in (data.get("provides") or [])],
            requires=[str(x) for x in (data.get("requires") or [])],
            permissions={k: [str(i) for i in (v or [])] for k, v in perms.items()},
            trust=trust,
        )

    def allowed_actions(self) -> set[str]:
        return set(self.permissions.get("actions") or [])

    def allowed_fs(self) -> set[str]:
        return set(self.permissions.get("fs") or [])

    def allowed_shell(self) -> set[str]:
        return set(self.permissions.get("shell") or [])

    def to_dict(self) -> dict:
        return {
            "name": self.name, "version": self.version, "entry": self.entry,
            "description": self.description, "provides": self.provides,
            "requires": self.requires, "permissions": self.permissions,
            "trust": self.trust,
        }


def load_manifest(path: str | Path) -> PluginManifest:
    p = Path(path)
    if not p.exists():
        raise PluginError(f"清单不存在：{p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PluginError(f"清单无法解析：{type(e).__name__}: {e}") from e
    return PluginManifest.from_dict(data)


class PluginRegistry:
    """已加载插件与其权限声明。运行时权限检查走 check_action。"""

    def __init__(self):
        self._plugins: dict[str, PluginManifest] = {}
        self._denials: list[dict] = []

    def load(self, manifest: PluginManifest) -> None:
        if manifest.name in self._plugins:
            raise PluginError(f"插件已加载：{manifest.name}")
        self._plugins[manifest.name] = manifest

    def get(self, name: str) -> PluginManifest | None:
        return self._plugins.get(name)

    def names(self) -> list[str]:
        return sorted(self._plugins)

    def describe(self) -> list[dict]:
        return [m.to_dict() for m in self._plugins.values()]

    def check_action(self, plugin: str, action: str) -> None:
        """插件调用动作前的权限检查。未声明即拒绝，并记录。"""
        m = self._plugins.get(plugin)
        if m is None:
            raise PluginError(f"未加载的插件：{plugin}")
        if action not in m.allowed_actions():
            self._denials.append({"plugin": plugin, "action": action, "reason": "not_declared"})
            if len(self._denials) > 200:
                self._denials = self._denials[-200:]
            raise PluginError(f"插件 {plugin} 未声明动作权限：{action}")

    def denials(self) -> list[dict]:
        return list(self._denials)


registry = PluginRegistry()
