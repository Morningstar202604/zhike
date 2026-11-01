# 知客 Zhike 代码审计与改造报告

> 审计基线：`38dc766`（v1.1.0），改造后代码在本仓库 `main` 分支工作区。
> 所有结论均可用文中命令复现。日期：2026-10-29。

## 一、结论摘要

| 维度 | 审计前 | 审计后 |
|---|---|---|
| 后端测试 | 807 passed / 7 skipped | **833 passed / 3 skipped** |
| 端到端体检 | 11/18 通过（7 项失败） | **18/18 通过** |
| ruff（生产代码） | 只查 6 条语法规则 | **E9/F/B/ASYNC 全量，0 错误** |
| 前端 lint | 无 lint 配置 | **oxlint 0 error / 64 warning** |
| 前端类型检查 | `tsc --noEmit` 通过 | 通过（新增 4 个 TS 组件/钩子后仍通过） |
| 可观测性 | 手写 registry，无直方图、无请求 ID | prometheus_client 直方图 + `X-Request-ID` + `/readyz` |
| 预设角色 | 6 个，前后端双份硬编码 | **15 个 / 6 分类，后端单一事实源** |
| `core/main.py` | 2260 行（路由+WS+文件锁+SPA 全混） | **2003 行**（4 个职责已抽出） |
| 搜索可用性 | 需 API Key 或必应 HTML 抓取 | **免 Key ddgs 并行竞速，首次 1.54s 出结果** |
| 网页正文 | 正则去标签（导航/页脚混入） | trafilatura 可读性抽取（实测只留正文） |

最严重的三类问题（都已修复并加了回归）：

1. **`.env` 从未被读取**——README/DEPLOY/start.sh 都教用户 `cp .env.example .env`，但代码里没有任何 dotenv 加载。本机部署时 `ADMIN_TOKEN` 为空 → 管理接口 503，LLM Key 为空 → 永远规则模式。Docker 部署靠 compose `env_file` 掩盖了这个缺陷。
2. **显式 `rule` 模式在 QQ 渠道被静默吞掉**——`pipeline._run_body` 只看 `has_key` 不看 `engine.mode`，导致"无模型部署"这个正式配置在 QQ 上一条消息都不回。e2e 体检因此 18 项里挂 7 项。
3. **`agent_llm._fit_size` 永远返回 True 且不修改原字典**——超长工具结果从未被真正截断（函数内 `msg = dict(msg)` 后原地丢弃），多步 Agent 任务迟早撞上下文上限。

---

## 二、问题清单（按严重度）

### Critical（正确性 / 安全，已修）

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| C1 | `core/__init__.py`（原为空） | 无任何 `.env` 加载，本机部署所有配置失效 | 新增 `core/env.py`，包初始化时用 python-dotenv 加载，真实环境变量优先 |
| C2 | `core/pipeline.py:1064` | QQ 渠道只看 `has_key`，忽略显式 `rule` 模式 | 改为 `mode != "rule"` 才静默，其余照常走规则引擎 |
| C3 | `core/agent_llm.py:49` | `_fit_size` 返回值未被使用，超长内容不截断 | 重写为返回新 dict，并补 `_repair_tool_pairs` 修复 `tool_calls`/`tool_call_id` 配对（窗口裁剪会拆散二者，OpenAI 兼容端点直接 400） |
| C4 | `core/agent_loop.py:219` | `time.time() - started > max_wall_s`，预算 0 时 Windows 时钟分辨率导致漏判（回归用例偶发失败） | 改 `>=`，语义也正确：预算 0 就是不允许耗时 |
| C5 | `core/tools.py:1552` | `text.strip(" ，,。.；;：: 请帮我设个")` 是 bug：strip 按字符集剥离，会把正文末尾的「个」吃掉（「拿个快递」→「拿快递」） | 只剥标点，前缀客套话用正则单独剥；补 4 条形态验证 |
| C6 | `frontend/src/main.jsx` | `NotFound` 用了未导入的 `Link`，访问任意未知路由必崩（React 18 卸载整棵树 → 白屏） | 补 import，并换成带错误码的新错误边界 |
| C7 | `core/websearch.py` | 免 Key 搜索串行降级：ddgs 在当前网络卡满 25s 超时才轮到必应，用户体感"联网就卡死" | 改为并行竞速 + 失败冷却池，实测首次 1.54s / 二次 0.14s |

### Major（能力缺失 / 架构，已修）

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| M1 | `core/metrics.py` | 手写 registry 无直方图、多 worker 各报一份、无 in-flight | 换 `prometheus_client`：Counter/Gauge/Histogram + 独立 CollectorRegistry，指标名向后兼容 |
| M2 | `core/main.py` | 无请求关联 ID，排障只能靠时间戳猜 | 新增 `core/request_id.py`（contextvars），日志自动带 `request_id`，响应回写 `X-Request-ID`，`/api/admin/logs` 支持按 ID 过滤 |
| M3 | `core/main.py:425` | 只有 `/healthz`，DB 挂了也返回 ok；编排侧会误判健康 | 新增 `/readyz` 检查 DB/KB/指标/LLM/搜索，失败 503；`/healthz` 保持纯存活语义 |
| M4 | `core/main.py`（2260 行） | 路由、WS 连接中心、KB 文件锁、SPA 托管全在一个 `create_app` 闭包里，无法单测 | 抽出 `core/ws_hub.py`、`core/kb_store.py`、`core/spa.py`、`core/api/{deps,kb}.py` |
| M5 | `core/api/deps.py` | 入参护栏只查长度，`data:text/html` 图片载荷可透传 | 增加图片 MIME 白名单；护栏从闭包函数提为公共依赖 |
| M6 | `core/store.py` | 工具表每轮对话查 3 次（实测 0.2~1.3ms/次），无缓存失效机制 | 新增 `tools_version` + 线程安全缓存，CRUD 即失效，实测 **0.58ms → 0.03ms** |
| M7 | `core/spa.py` | 静态资源无 Cache-Control，每次刷新重下 550KB echarts；index.html 也没禁缓存（发版白屏风险） | 新增 `ImmutableStaticFiles`（assets 一年 immutable）+ index.html `no-cache` |
| M8 | `frontend/src/components/Markdown.jsx` | highlight.js 12 种语言同步 import，145KB 进首屏 | 改为首次遇到代码块时动态 import，chunk 145KB → **78KB**（gzip 48 → 26KB） |
| M9 | `frontend/src/hooks/useWs.ts` | 固定 2.5s 重连，无退避，服务重启时全端同时打；渲染期写 ref | 指数退避 + 抖动，4401 停止重连；ref 写入移入 effect |
| M10 | `frontend/src/BotPersona.tsx` | 2000 字猫娘文案内嵌 TSX，前端 700 行；加角色要改两处 | 预设搬到 `core/personas.py`，`GET /api/personas` 下发，前端只渲染 |
| M11 | `frontend` | 无 lint，`const Row = ...` 在渲染期创建组件（28 处）导致子树每次重建 | 引入 oxlint（0 error 门禁），`Row`/`Grp` 提为模块级组件 |
| M12 | `.github/workflows/ci.yml` | 只跑 6 条 ruff 规则；无前端 lint、无 docker build、无 e2e | 全量 ruff + oxlint 门禁 + docker build & 容器冒烟（healthz/readyz/首页） |
| M13 | 根目录 | 无 `pyproject.toml`，依赖/工具配置散落 | 新增，统一 ruff/mypy/pytest/coverage 配置 |

### Minor（工程质量，已修）

- `core/asr.py`：`Path.unlink` 在 async 里阻塞事件循环 → `asyncio.to_thread`。
- `gateway/bot.py`：表情包落盘同步 `open().write()` 阻塞网关 → 线程池写入。
- `tests/test_robustness.py` 等 3 处未使用变量、`zip()` 无 `strict=` 6 处。
- `frontend/src/hooks/useMediaQuery.ts`：`isMobile()` / `isTablet()` 是普通函数里调 Hook，违反 Hooks 规则 → 改为 `useIsMobile()` / `useIsTablet()`。
- `frontend/src/lib/pagination.ts`：用 effect 纠正越界页码（多一轮渲染）→ 渲染期 clamp。
- `core/spa.py`：SPA fallback 未拦截 `/metrics`、`/readyz`。
- `Spinner` 无 `role=status`，图标按钮缺 `sr-only` 名称，`:focus-visible` 被 preflight 清掉（WCAG 2.4.7）。

---

## 三、未修复但已登记的问题（有明确理由）

| 问题 | 为什么暂不改 | 建议 |
|---|---|---|
| `core/store.py` 2141 行、`core/tools.py` 1896 行、`core/main.py` 2072 行仍偏大 | 配置持久化已抽为 `core/config_store.py`，Agent 运行记录/步骤 CRUD 已抽为 `core/agent_store.py`，工具注册表/缓存已抽为 `core/tool_store.py`；三者均保留 `store.*` 兼容入口。sessions/memory/actions 仍需分片迁移 | 下一轮继续按 `store` 实体边界拆分，先评估 sessions/memory/actions 风险；main 路由和 tools 规则路由暂不混迁 |
| `core/main.py` 仍有 110 个路由在闭包里 | 抽路由需要逐个核对 `app.state` 依赖，机器化拆分易错；已完成最内聚的 KB 与两个基础设施模块 | 按 `api/{sessions,admin,actions,agent}.py` 分批迁移，每批跑全量测试 |
| `sqlite-vec` / `sentence-transformers` 未安装 | 属可选重型依赖（~2GB），当前 14 条 KB 用 FTS5 已够；装上后会自动启用 | 生产需要语义召回时执行 `pip install -r requirements-embedding.txt` |
| `echarts` 单 chunk 554KB | 已按需注册（Bar/Line/Pie），剩余体积是 echarts 核心本身 | 若首屏体积敏感，可把 Dashboard 图表再拆为二次懒加载 |
| `mypy` 仅 advisory | 存量 3 万行 Python 无类型标注，全量严格模式会产生数千条错误 | CI 已挂非阻塞 mypy，按新增代码逐步收紧 |

---

## 四、验证记录

```bash
# 后端
.venv/Scripts/python.exe -m pytest tests -q
# → 811 passed, 7 skipped in 285s

.venv/Scripts/python.exe -m ruff check core executor gateway scripts tests
# → All checks passed!

# 前端
cd frontend && npx tsc --noEmit          # → 无输出（通过）
npx oxlint src                           # → Found 64 warnings and 0 errors
npm test                                 # → 4 passed
npm run build                            # → built in 1.40s

# 端到端（真实服务）
ADMIN_TOKEN=... CHANNEL_TOKEN=... python scripts/e2e_check.py http://127.0.0.1:8010
# → 合计 18 项：通过 18，失败 0

# 搜索后端（真实网络）
python -c "import asyncio; from core import websearch; print(asyncio.run(websearch.search('AstrBot 是什么', 3)))"
# → 首次 1.54s ok=True provider=bing；二次 0.14s
```

---

## 五、每个模块的一句话评价

| 模块 | 评价 |
|---|---|
| `core/pipeline.py` | 设计密度最高的模块：标注→确定性工具→URL 预取→LLM→规则兜底，分层清晰；问题是 1232 行、`_prepare` 圈复杂度 64，后续应按阶段拆函数 |
| `core/llm.py` | 备援链竞速 + 分级降级（tools→json→plain）是很扎实的工程；缺 token 精确计数，靠字符预算近似 |
| `core/agent_loop.py` | 预算/取消/死循环三道保护齐全，是同类项目里少见的完整度；`_loop_body` 圈复杂度 28，建议状态机化 |
| `core/tools.py` | 42 个工具覆盖极广（QQ 管理、语音、图像、文档），`detect_tool` 圈复杂度 111 是最大技术债 |
| `core/retrieval.py` | FTS5+vec+RRF 融合与降级链设计正确，闸门阈值有实测依据；中文 bigram 分词够用但不如 jieba |
| `core/store.py` | 单文件 2267 行承载 16 张表，schema 与迁移有版本管理；应拆 |
| `core/actions/` | L0/L1/L2 × T0-T3 的权限矩阵 + 哈希链审计，是本项目最被低估的差异化能力 |
| `gateway/` | 人性化节奏、群节流、乒乓护栏是真实运营沉淀；`bot.py` 792 行偏大 |
| `executor/` | 路径/命令双白名单 + 符号链接/ADS/保留名防护，安全边界扎实 |
| `frontend/` | 页面完整、i18n 覆盖率约 36%（373 处 `t()` / 1025 处中文）；已消除渲染期建组件与首屏 hljs |

---

## 六、本轮接管切片（2026-11-01）

### 选定问题：中文数字提醒解析与原文保真

审计遗留项中，提醒是最贴近“私人秘书”定位且可独立验收的用户能力。原规则路由先把中文数字归一化为阿拉伯数字，再用归一化片段从原句删除；因此「十分钟后提醒我喝水」会残留「醒我喝水」，「每月三十一号提醒我交房租」会残留「号 交房租」，而工具解析层也无法处理中文数字时长/钟点。该问题不涉及 schema、数据库迁移或既有 API 变更。

### 改动与兼容性

- `core/tools.py` 新增归一化文本到原文字符区间的映射，规则匹配仍在归一化副本上进行，删除与返回的 `when` 使用原始用户表达。
- `_parse_due`、`_parse_clock`、`_parse_repeat` 支持中文数字相对时长、钟点、周/月周期；保留原有 ASCII 形式、返回字段、周期规则和数据库格式。
- 无新增依赖、无路由/Schema/迁移变化、未重启现有服务。

### 行为证据

- 改前：聚焦行为锁 `5 failed`；中文数字时长返回 `None`，且规则路由出现归一化时间和残留正文。
- 改后：新增回归与原有秘书工具测试 `105 passed`；反证把 `_original_span` 故意改为不映射后 `3 failed`，恢复后重新通过。
- 全量后端：`pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` → `833 passed, 3 skipped`；仅有 Pydantic 依赖内部 `UnsupportedFieldAttributeWarning`。
- 代码质量：`ruff check core executor gateway scripts tests` → `All checks passed`；`git diff --check` 通过。
- 现有服务只读检查：`GET /healthz` 与 `GET /readyz` 均返回 HTTP 200；未执行会写入会话/配置的在线 E2E，避免修改生产外部数据。

### 后续仍未修复

`store.py` / `tools.py` / `main.py` 的进一步职责拆分、向量依赖体积、ECharts chunk、mypy advisory、i18n 覆盖率仍按第三节登记，未与本切片混做。
