"""渠道适配器层：统一接口 + OneBot 实现 + 事件总线 + 插件清单。"""
from gateway.adapters.base import (  # noqa: F401
    CAP_DELETE_MSG, CAP_GROUP_ADMIN, CAP_GROUP_MEMBER_LIST, CAP_MESSAGE_HISTORY,
    CAP_SEND_FILE, CAP_SEND_IMAGE, CAP_SEND_TEXT, ChannelAdapter, NormalizedMessage,
    SendResult, UnsupportedCapability, adapter_info, require_capability,
)
from gateway.adapters.bus import EventBus, bus, registry  # noqa: F401
from gateway.adapters.onebot11 import OneBot11Adapter  # noqa: F401
from gateway.adapters.plugins import PluginManifest, PluginRegistry  # noqa: F401
