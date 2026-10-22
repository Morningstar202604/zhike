# QQ 登录与联调（NapCat + OneBot v11）

本文记录 **实测过** 的接入路径，不是通用教程。每一步都注明失败时看什么。

## 0. 前置

| 项 | 要求 | 检查命令 |
|---|---|---|
| Docker | 已安装并能 pull 镜像 | `docker version` |
| 一个 QQ 号 | 建议用小号/机器人号，**不要用主号** | — |
| 手机 | 装了 QQ（扫码登录用） | — |

## 1. 启动 NapCat（WS 服务端模式）

```bash
docker rm -f zk-napcat 2>/dev/null

docker run -d --name zk-napcat \
  -e MODE=ws \
  -e WEBUI_TOKEN=zhike-local \
  -p 127.0.0.1:6099:6099 \
  -p 127.0.0.1:3001:3001 \
  mlikiowa/napcat-docker:latest
```

- `MODE=ws` 是关键：它让 NapCat 生成 `websocketServers`（监听 3001），
  而默认模板是 `nonebot`（主动外连），方向相反、网关连不上。
- 两个端口都只绑 `127.0.0.1`：WebUI 与 OneBot 都不该暴露到公网。

## 2. 扫码登录

```bash
python scripts/qq_login.py --timeout 300
```

脚本会：取容器内最新二维码 → 终端画出来（手机扫屏幕）→ 打印官方解码链接兜底 →
每 3 秒检查登录状态与 3001 端口，成功/失败都给明确结论。

如果终端二维码画得糊，直接打开脚本打印的 PNG 路径，或用解码链接在手机浏览器打开。

**注意**：二维码约 2 分钟过期。脚本检测到失效会自动重新取图；如果彻底超时，重跑脚本即可。

## 3. 启动网关

网关自己会起一个内部 HTTP 端口（默认 8011）供 core 调 `/internal/action`；
`PORT`/`HOST` 可覆盖（compose 里是 `HOST=0.0.0.0` `PORT=8011`）。

```bash
# Windows PowerShell
$env:ONEBOT_WS_URL="ws://127.0.0.1:3001"
$env:CORE_BASE_URL="http://127.0.0.1:8010"
$env:CHANNEL_TOKEN="<与服务端一致>"
python gateway/bot.py

# Linux / macOS
ONEBOT_WS_URL=ws://127.0.0.1:3001 \
CORE_BASE_URL=http://127.0.0.1:8010 \
CHANNEL_TOKEN=... python gateway/bot.py
```

网关启动后日志应出现：

```
[gateway] adapter bound: onebot_v11
[gateway] heartbeat started, interval=30s
```

成功标志：`curl -H "X-Admin-Token: $ADMIN" localhost:8010/api/gateway/health`
返回 `{"online": true, "connected": true, "self_id": "<你的QQ号>"}`。

> 网关连不上 core 的 `GATEWAY_ACTION_URL` 时，动作类接口会 503，但 QQ 对话不受影响。

## 4. 联调验证（按顺序，前一步不过不要往下走）

| # | 动作 | 期望 | 失败时看 |
|---|---|---|---|
| 1 | 私聊机器人发「你好」 | 收到回复 | `docker logs zk-napcat --tail 50` 是否收到事件；网关日志是否 `ingest failed` |
| 2 | 群里 @机器人 说「我的订单什么时候发货」 | 群内回复且引用 KB001 | 群里是否真的 @ 到（`to_me` 判定）；`mapping.from_onebot_v11` 单测已覆盖 |
| 3 | 私聊发「帮我转人工」 | 回复转接话术 + 控制台待接管 +1 | `/api/operator/queues` |
| 4 | 私聊发一张截图 | 规则模式回复「暂无法理解图片，转人工」 | 视觉模型未配属预期行为 |
| 5 | 控制台坐席台接管 → 回复 | 访客 QQ 实时收到坐席消息 | 访客 WS 是否在线（坐席端 `/ws/operator`） |

## 5. 稳定性设计（已实现，不是计划）

| 机制 | 位置 | 作用 |
|---|---|---|
| 消息去重 LRU(2000) | `gateway/bot.py:_dedup` | 协议端重连会重放最近事件，不去重会重复回复 |
| 同会话串行锁 | `gateway/bot.py:_chat_lock` | 群友连发多条时回复顺序与提问一致 |
| 全局并发上限 8 | `GATEWAY_MAX_CONCURRENT` | 群里刷屏/全员 @ 时不打爆核心与模型 |
| ingest 超时 90s | `INGEST_TIMEOUT_S` | LLM ReAct + 工具调用可能超过 40s |
| 网络/5xx 重试一次 | `gateway/bot.py:_post_ingest` | 瞬时抖动不丢消息；4xx 不重试（业务拒绝） |
| 发送失败重试一次 | `handle()` | 协议端偶发超时 |
| 心跳 30s + 退避日志 | `_heartbeat_loop` | 核心不可用时只在前 3 次与每 10 次告警，不刷屏 |
| 核心不可达兜底话术 | `handle()` except | 用户不会面对沉默 |

## 6. 常见失败与处置（都是实测踩过的）

| 现象 | 原因 | 处置 |
|---|---|---|
| 网关报 `did not receive a valid HTTP response` | NapCat 没登录/没监听 3001 | 先完成扫码；确认 `onebot11.json` 里 `websocketServers[0].port=3001` |
| 网关握手 403 | NapCat 开了 access token | 两边填同一个 `ONEBOT_ACCESS_TOKEN` |
| 扫码后一直「等待」 | 二维码过期 | 重跑 `scripts/qq_login.py` |
| 群里 @ 没反应 | 消息没真正 @ 到机器人 | 手动输入 `@机器人名` 再选；或私聊测试 |
| 回复重复 | 旧版本没有去重 | 升级后重启网关（已修） |
| 接管后机器人还在回 | 旧版本会话复用只看 ongoing，接管后新消息落进新会话 | 升级后重启 core（已修：接管后消息只进坐席台） |
| 控制台动作页显示执行器离线 | 执行器未启动，或 `EXECUTOR_TOKEN` 与 core 不一致 | `python -m executor.main`，核对两边令牌 |
| 同一群消息顺序乱 | 旧版本并发处理 | 升级后重启（本轮已修，同会话串行） |
| `docker cp` 找不到 qrcode.png | 还没进入登录流程 | 等 10 秒，或看 `docker logs` 是否已打印二维码 |

## 7. 安全提醒

- QQ 机器人号建议单独注册，**不要用有支付/实名关系的主号**。
- NapCat WebUI token 不要用弱口令；6099 端口不要映射到 `0.0.0.0`。
- `CHANNEL_TOKEN` 必须设置且与核心一致，否则任何人都能伪造渠道消息。
