# 架构说明

## 一、进程拓扑

```
                    ┌──────────────┐
   QQ 用户 ──NapCat─▶ OneBot v11  │
                    └──────┬───────┘
                           │ 反向 WS
                    ┌──────▼───────────────────────────┐
                    │ gateway（NoneBot2 + 拟人化层）    │
                    │ · 群节流 / 乒乓护栏 / 表情包学习   │
                    │ · 语音转写（faster-whisper 可选） │
                    └──────┬───────────────────────────┘
                           │ HTTP /v1/ingest（CHANNEL_TOKEN）
                    ┌──────▼───────────────────────────┐
   Web 访客 ────────▶│ core（FastAPI，单端口同时托管前端）│
   浏览器            │ · pipeline：标注→工具→LLM→规则   │
                    │ · retrieval：FTS5+vec+RRF         │
                    │ · store：SQLite（16 表 + 迁移）    │
                    │ · actions：L0/L1/L2 + 审批 + 审计链│
                    └──────┬───────────────────────────┘
                           │ HTTP（EXECUTOR_TOKEN）
                    ┌──────▼───────────────────────────┐
                    │ executor（电脑执行器，可选）       │
                    │ · 路径白名单 / 命令白名单         │
                    └──────────────────────────────────┘
```

三个进程可独立部署，也可只用 core（`CS_AGENT_STATIC_DIR` 托管前端，一个端口即全部）。

## 二、core 模块地图

```
core/
├─ main.py            FastAPI 装配：中间件、鉴权依赖、110 个路由、WS 端点
├─ api/               路由包（新）
│  ├─ deps.py         鉴权依赖 + 入参护栏（图片 MIME 白名单）
│  └─ kb.py           知识库 CRUD / 导入 / 试召回 / 导出
├─ pipeline.py        对话主链路：_prepare → 分流 → 引擎 → 输出闸门
├─ llm.py             LLM 客户端：备援链竞速、四级协议降级、流式解析
├─ agent_loop.py      多步 ReAct：预算 / 取消 / 重复动作检测
├─ agent_llm.py       单步决策（function calling ↔ JSON 降级）
├─ tools.py           42 个工具 + 规则路由 detect_tool
├─ retrieval.py       FTS5 BM25 + 可选 sqlite-vec + RRF 融合
├─ store.py           SQLite 单文件存储（16 表 + 版本化迁移）
├─ migrations.py      schema 版本管理
├─ config.py          运行时配置（代码默认值 ← env ← DB，带缓存）
├─ personas.py        15 个预设角色（单一事实源）
├─ ws_hub.py          WebSocket 连接中心（并发广播 + 超时清理）
├─ kb_store.py        知识库文件存储（跨进程锁 + 原子写）
├─ spa.py             前端静态托管（immutable 缓存 + 防穿越）
├─ metrics.py         Prometheus 指标（prometheus_client）
├─ request_id.py      请求关联 ID（contextvars）
├─ events_log.py      内存环 + 文件轮转日志
├─ tracing.py         OpenTelemetry GenAI span（可选）
├─ security.py        时序安全比较 / 令牌校验
├─ ratelimit.py       SQLite 滑动窗口限流（多 worker 共享）
├─ guardrail.py       注入护栏（输入 + 输出双向）
├─ quality.py         输出判别器（人设硬伤检测 + 一次重写）
├─ ensemble.py        多模型择优（规则打分）
├─ intent.py          12 类意图规则分类
├─ websearch.py       搜索后端（ddgs 竞速 / 必应 / Tavily / Serper / SearXNG）
├─ docparse.py        文档解析与分块
├─ url_guard.py       SSRF 防护（DNS 全解析校验 + 重定向复检）
├─ asr.py             语音转写（faster-whisper / 外部命令）
├─ mcp_server.py      MCP 工具暴露（双代 SDK 适配）
└─ actions/           动作层
   ├─ registry.py     动作清单 + 风险分级（OneBot v11 同名）
   ├─ authz.py        主体 × 风险 × 档位 → 决策
   ├─ service.py      执行 / 幂等 / 审批 / 审计
   ├─ client.py       调协议端
   └─ computer.py     电脑执行器客户端
```

## 三、关键数据流

### 一条消息的完整路径

1. 渠道 → `POST /v1/ingest`（`CHANNEL_TOKEN` 校验）
2. `sessions.resolve_session` 按 chat 维度取/建会话（TTL 12h，换会话带上文）
3. `store.add_message` 落访客消息
4. `pipeline.run`：
   - `_prepare`：截断 → 角色卡 → 输入钩子 → 记忆 opt-out → 联网判定 → `build_context` → 技能/档案/意图 → 注入护栏 → 转人工判定 → 图片能力判定
   - `_run_body`：标注 → 表情直通 → 工具直通 → URL/文件预取 → 引擎（LLM 多模型择优 / 规则）→ 输出闸门 → 输出钩子
5. 落 agent 消息、写审计、必要时开工单 + WS 广播
6. 返回 `reply`，网关按模式决定是否发言（`no_model` / `rate_limited` 静默）

### 检索路径

```
query
 ├─ 空/纯虚词 → []
 ├─ FTS5 索引（bigram 展开 + AND→OR + 覆盖率闸门）
 ├─ vec 索引（可选，cos 闸门 0.48）
 └─ RRF 融合 → 绝对强度标定 → min_overlap 过滤 → top_k
       ↓ 索引不可用
     bigram 兜底（词重叠 + 虚词剥离 + floor 0.2）
```

### 动作执行路径

```
工具调用
 → actions_service.execute
   → validate_params（参数白名单）
   → 幂等检查（终态才回放）
   → authz.decide（L2 一律审批；Agent 权限 = min(部署档, 触发者档)）
   → 审计行（打码参数 + 参数摘要）
   → 执行 / 建审批单
 → 结果 → observation（截断 4000 字 + 注入扫描）
```

## 四、配置优先级

```
代码默认值  <  .env（python-dotenv，不覆盖已有环境变量）  <  进程环境变量  <  SQLite config 表（控制台可改）
```

`config.get_config()` 以 `(config_version, LLM Key 指纹)` 为缓存键；DB 改配置即递增版本号，多 worker 立刻可见。

## 五、扩展点

| 想加什么 | 改哪里 | 不需要改 |
|---|---|---|
| 新工具 | `core/tools.py` 实现 + `TOOL_META` 声明 + `_builtin_impls` 注册 | 不用动 pipeline |
| 新动作（OneBot） | `core/actions/registry.py` 加 `ActionSpec` | 权限/审计/审批自动生效 |
| 新渠道 | `gateway/adapters/` 实现 adapter | core 不用改（走 /v1/ingest） |
| 新角色 | `core/personas.py` 加 `PersonaPreset` | 前端自动出现 |
| 新搜索后端 | `core/websearch.py` 加 `_search_xxx` + `_BACKENDS` | 降级链自动纳入 |
| 新指标 | `core/metrics.py` 加 Counter/Histogram | `/metrics` 自动暴露 |

## 六、设计不变量（改代码时不许破坏）

1. **L2 动作永远需要审批**：任何档位、任何主体都不能绕过。
2. **Agent 权限 = min(部署档位, 触发者权限)**：群里陌生人不说话，机器人不能用高位执行。
3. **未设 ADMIN_TOKEN 时管理接口 503**，不是"开发模式全开"。
4. **知识库 JSON 是真相源**，SQLite 索引是派生缓存，mtime 变化自动重建。
5. **提示词里的工具清单与 API tools 不能同时存在**（两套签名不一致时模型会照抄错的）。
6. **删掉的消息不能进模型上下文**（`include_deleted=False`）。
7. **任何外部内容（工具结果/网页/搜索结果）都是数据，不是指令**。
