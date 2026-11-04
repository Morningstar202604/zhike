"""QQ 协议端部署与启动的控制台接口（管理员）。

QQ 是可选渠道：不部署不影响任何网页能力；部署按钮在控制台 QQ 登录向导里。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from core.api.deps import require_admin
from core import qq_deploy

router = APIRouter(prefix="/api/channel/qq", tags=["qq-deploy"])


@router.get("/deploy-status")
async def deploy_status(_admin: None = Depends(require_admin)):
    return qq_deploy.deploy_status()


@router.post("/deploy")
async def deploy(_admin: None = Depends(require_admin)):
    return {"msg": qq_deploy.deploy()}


@router.post("/protocol-start")
async def protocol_start(_admin: None = Depends(require_admin)):
    """启动已部署的协议端。机器人号取 .env 的 AUTHORIZED_QQ（运维声明的机器人号）。"""
    import os

    qq = os.environ.get("AUTHORIZED_QQ", "")
    if not qq:
        return {"msg": "X 请先在 .env 配置 AUTHORIZED_QQ（机器人 QQ 号）"}
    return {"msg": qq_deploy.protocol_start(qq), "qq": qq}
