# 升级方案与架构决策（UPGRADE PLAN）

> 项目：知客 Zhike · 在线客服 Agent（对接 QQ 群 / 微信）
> 旧仓：gitcode.com/badhope/online-cs-agent（MIT，2026-09-22 起，27 个测试通过）
> 新仓：zhike（本仓库，MIT）
> 日期：2026-09-28

## 一、旧仓底层审计（为什么要重做）

**技术栈**：FastAPI + sqlite3 直用（无 ORM）+ 手写 bigram 检索 + OpenAI SDK + React/Vite 前端。
**做对的地方**：模块划分清晰；护栏/工具/配置是数据驱动的；无 LLM Key 时优雅降级；27 个测试可跑通；MIT 协议干净。

**关键缺陷（按严重度）**：

| # | 问题 | 位置（旧仓） | 影响 |
|---|---|---|---|
| 1 | **没有任何渠道层**：只有 Web 聊天页，不接 QQ/微信 | 全仓 | 产品目标（对接 QQ 群/微信）完全未实现 |
| 2 | LLM 调用是同步 `client.chat.completions.create`，却在 `async def post_message` 里直接调用 | backend/main.py:132 | LLM 请求期间整个事件循环被卡死，所有会话/WS 全部阻塞 |
| 3 | 操作员/管理/配置/工具/知识库全部接口**零鉴权**，CORS `allow_origins=["*"]` | backend/main.py:13-18 | 任何人可接管会话、改配置、读全部访客记忆与注入日志 |
| 4 | 图片参数接收后**根本没入库**（`image_b64_placeholder` 未写入 INSERT），多模态是假的 | backend/main.py:130, backend/storage.py:159-167 | 用户发的图直接丢失 |
| 5 | 内置注入规则 `(pretend|假装|现在你)` 过宽 | backend/storage.py:125 | 「现在你们几点上班」这类正常消息会被误拦 |
| 6 | ReAct 名不副实：`react_steps` 只写进提示词，实际是单轮调用最多一次工具 | backend/agent_core.py:421-510 | 复杂问题无法多步推理 |
| 7 | LLM 异常全部静默吞掉返回 None | backend/agent_core.py:509-510 | 线上排障困难 |
| 8 | WS 连接注册表、限流能力缺失；多进程部署即失效 | backend/main.py:43-78 | 无法水平扩展 |
| 9 | 测试依赖 `importlib.reload` 处理 env，DB 路径在 import 时固化 | backend/test_agent.py:9-18 | 测试间隔离脆弱 |

**结论**：旧仓是一个方向正确的演示品，不是可上线项目。重做（而非修补）成本更低、边界更干净；旧仓代码按 MIT 以移植方式复用。

## 二、新架构

```
                    ┌────────────────────────────────────────────┐
 QQ 群/私聊 ──► NapCat(协议端) ──OneBot v11 WS──► NoneBot2 网关 ──┤
                                              (gateway/)        │ HTTP /v1/ingest
                                                                ▼
                                                     ┌─────────────────────┐
 Web 访客 ──────────────── /api/* + /ws/* ────────► │  core (FastAPI)     │
 React 工作台（旧前端复用）                          │  护栏→检索→工具→LLM  │
                                                     │  会话/记忆/工单/坐席  │
                                                     └─────────────────────┘
```

- **core（我们的代码，MIT）**：摄取入口 `/v1/ingest` + 旧版 Web API 全兼容（前端零改动）+ 管线异步化 + 管理面鉴权（ADMIN_TOKEN）+ 每身份限流 + 渠道会话（按群/私聊维度，空闲 TTL 自动新建）。
- **gateway（NoneBot2，MIT）**：官方 OneBot v11 适配器连 NapCat；群聊仅响应 @机器人；私聊全响应。事件映射是纯函数（`gateway/mapping.py`），脱离 NoneBot 可单测。
- **前端**：旧仓 React 工作台原样复用（vite 代理已指向 core:8000）。
- **协议端**：QQ 走 NapCat（OneBot v11）；部署用 `deploy/docker-compose.yml`。

**为什么不整包合并 AstrBot**（用户点名可考虑）：AstrBot 是 AGPL-3.0——把它的源码合并进本项目并分发，整个项目必须跟着 AGPL 开源；且它是通用聊天机器人框架（~37k star 全家桶），客服场景只用到其渠道适配一小块，引入它等于把项目架构绑死在别人的内核上。**可选路线**：若将来想要现成的多平台管理面板，可以把本项目 core 当作 AstrBot 的插件后端（HTTP 对接，进程隔离），AGPL 不传染到本仓代码——这条列入 Roadmap P3。

## 三、渠道方案与许可证矩阵（2026-09 核实）

| 方案 | License | QQ 群 | 微信 | 维护 | 结论 |
|---|---|---|---|---|---|
| NoneBot2 + 官方 OneBot 适配器 | MIT | ✅ OneBot v11 | 间接 | 活跃 | **✅ 采用（网关层）** |
| Koishi + 适配器 | MIT（LICENSE 原文已核） | ✅ | 公众号等 | 活跃 | 备选（TS 栈） |
| NapCat（协议端） | **自定义非商用许可**（禁止商用、限制改版公开，搜索核实，LICENSE 原文本机网络无法直连 GitHub，部署前必须自行复核原文） | ✅ NTQQ | - | 活跃 | ✅ 个人/内部用；商用前需授权评估 |
| QQ 官方机器人 API | 官方 | ✅ 群聊需上线审核，以 @ 为主，沙箱群可测 | - | 官方 | ✅ 合规路线，Roadmap P2 接入（NoneBot 官方有 QQ 适配器） |
| AstrBot 整包 | AGPL-3.0 | ✅ | ✅ | 活跃 | ⚠️ 只可进程隔离对接，不可合码 |
| 个人微信协议（wechaty/chatgpt-on-wechat 等） | 未逐项核实（本项目不采用） | - | 个人号 | 一般 | ⛔ 高封号风险，本项目明确不走 |
| 企业微信 / 微信客服官方 API | 官方 | - | ✅ | 官方 | ✅ 微信合规路径，Roadmap P2 |

**合规红线（本项目的决策）**：
1. QQ 群默认走 NapCat：个人/内部使用可接受，但**商用必须先评估 NapCat 自定义许可与腾讯风控**；合规优先的场景改走 QQ 官方机器人 API。
2. 微信**不做个人号协议**；走企业微信「微信客服」/公众号客服接口（官方 API，零封号风险）。
3. 本仓代码保持 MIT；任何 AGPL/GPL 组件只允许以独立进程 + HTTP 边界接入。

## 四、MVP 已交付

- `core/`：管线（护栏→上下文→规则/LLM）、`/v1/ingest` 渠道摄取、旧 Web API 全兼容、管理面令牌门禁、滑动窗口限流、渠道会话 TTL、异步 LLM（httpx，OpenAI 兼容端点，Key 缺失/失败自动降级规则+检索）、图片路径修复、过宽注入规则移除。
- `gateway/`：OneBot v11 事件映射（纯函数，9 个单测）+ NoneBot2 入口。
- `frontend/`：旧 React 工作台原样复用。
- `deploy/docker-compose.yml`：NapCat + core + gateway 编排（**本机无 Docker 未实跑**，字段以 NapCat 官方文档为准）。
- 测试：35 个用例全绿（`python -m pytest tests -q`）+ uvicorn 真机冒烟（healthz / QQ 群 ingest / 转人工工单）。

## 五、Roadmap

**P1（下一步，让它在真实 QQ 群跑起来）**
1. 起一个 QQ 小号 + NapCat，compose 起 core+gateway，群内 @ 实测。
2. 设置 ADMIN_TOKEN 并给前端工作台加令牌输入。
3. LLM 接入真实 Key（DeepSeek/GLM/Qwen 任一 OpenAI 兼容端点），关闭静默降级观察日志。

**P2（完整客服产品）**
4. QQ 官方机器人 API 适配（合规公域场景）。
5. 企业微信「微信客服」回调接入（微信合规路径）。
6. RAG 升级：可选 embedding 检索（bge/Qwen embedding，OpenAI 兼容接口），bigram 保留为降级路径。
7. LLM 流式输出（SSE）+ 真多步 ReAct（步数上限落进执行循环）。
8. Redis 限流/会话 + 多 worker 部署。

**P3（生态）**
9. AstrBot 插件形态对接（进程隔离，保持 MIT 仓干净）。
10. 多租户/多店铺知识库隔离；工单系统对接（如自建 or Chatwoot MIT）。

## 六、安全部署清单（上线前必查）

- [ ] `ADMIN_TOKEN` 已设置（否则管理面裸奔，启动日志会告警）
- [ ] CORS 收紧为工作台域名（当前为开发态 `*`，在 `core/main.py` create_app 中改）
- [ ] 反向代理（nginx/caddy）+ HTTPS
- [ ] NapCat 小号与主号隔离，频率上限（RATE_LIMIT_MAX）按群规模调低
- [ ] LLM Key 只从环境变量注入，不进代码/日志/DB（框架已保证）
- [ ] 商用前复核 NapCat LICENSE 原文与腾讯平台规则
