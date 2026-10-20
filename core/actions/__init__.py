"""动作层对外接口：注册表、权限判定、执行服务、网关客户端、电脑操作。"""
from core.actions import authz, client, computer, registry, service  # noqa: F401

# 电脑操作动作并入主注册表：与 QQ 动作共享同一套权限/审批/审计
registry.register_many(computer.COMPUTER_ACTIONS)
