# 部署与验证记录

> 本机实测环境：Windows 11 + Python 3.12.12 + Node 24.20 + pnpm 12.15
> 记录日期：2026-10-29

## 一、一键部署（本机 / Linux 通用）

```bash
git clone https://gitcode.com/badhope/zhike.git
cd zhike

# 1) Python 环境（项目要求 3.12+）
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt

# 2) 配置（现在会被真正读取——此前无代码加载 .env）
cp .env.example .env
# 至少填两个令牌；不填 LLM Key 也能跑（规则 + 检索模式）
python - <<'PY'
import secrets, pathlib
p = pathlib.Path(".env"); s = p.read_text(encoding="utf-8")
s = s.replace("ADMIN_TOKEN=", f"ADMIN_TOKEN={secrets.token_hex(16)}", 1)
s = s.replace("CHANNEL_TOKEN=", f"CHANNEL_TOKEN={secrets.token_hex(16)}", 1)
p.write_text(s, encoding="utf-8")
PY

# 3) 前端构建
cd frontend && npm ci && npm run build && cd ..

# 4) 启动（单端口 = 控制台 + API）
.venv/Scripts/python.exe -m uvicorn core.main:app --host 127.0.0.1 --port 8010
# Linux/macOS 用 .venv/bin/python
```

打开 http://127.0.0.1:8010 即控制台。

## 二、Docker 部署

```bash
docker compose -f deploy/docker-compose.yml up -d --build
# 服务：core(8010) / gateway(8011) / executor(8020, 仅内网) / napcat(WebUI 127.0.0.1:6099)
```

健康检查用 `/healthz`（存活）；编排摘流用 `/readyz`（就绪，检查 DB/KB/指标）。

## 三、验证清单（本次实测输出）

### 3.1 环境与依赖

```
$ uv venv --python 3.12 .venv
Using CPython 3.12.12
$ uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt
（安装成功，含新增 prometheus-client / tenacity / trafilatura / ddgs / python-dotenv）
```

### 3.2 后端测试

```
$ .venv/Scripts/python.exe -m pytest tests -q
811 passed, 7 skipped, 3 warnings in 285.52s
```

### 3.3 静态检查

```
$ .venv/Scripts/python.exe -m ruff check core executor gateway scripts tests
All checks passed!

$ cd frontend && npx tsc --noEmit
（无输出 = 通过）

$ npx oxlint src
Found 64 warnings and 0 errors.
```

### 3.4 前端构建

```
$ npm test
Test Files  1 passed (1)
     Tests  4 passed (4)

$ npm run build
dist/assets/echarts-*.js     554.32 kB │ gzip: 187.81 kB
dist/assets/vendor-*.js      264.40 kB │ gzip:  85.08 kB
dist/assets/Markdown-*.js     77.87 kB │ gzip:  26.37 kB   ← 改造前 145.69 kB / 47.98 kB
dist/assets/Settings-*.js     71.65 kB │ gzip:  19.42 kB
✓ built in 1.40s
```

### 3.5 服务启动 + 探针

```
$ curl -s http://127.0.0.1:8010/healthz
{"ok":true}

$ curl -s http://127.0.0.1:8010/readyz
{"ok":true,"checks":{
  "db":{"ok":true,"schema_version":16},
  "kb":{"ok":true,"entries":14},
  "metrics":{"ok":true,"backend":"prometheus_client"},
  "llm":{"configured":false},
  "search":{"configured":true}}}

$ curl -s http://127.0.0.1:8010/metrics | head
# HELP zhike_http_request_duration_seconds HTTP request latency in seconds.
# TYPE zhike_http_request_duration_seconds histogram
zhike_http_request_duration_seconds_bucket{le="0.005",method="GET",path="/healthz"} 1.0
```

### 3.6 端到端体检（18 项）

```
$ ADMIN_TOKEN=... CHANNEL_TOKEN=... python scripts/e2e_check.py http://127.0.0.1:8010
[PASS] 20 健康检查 /healthz + /api/agent/health      mode=rule_react kb=14 tools=42
[PASS] 01 知识库检索命中（物流）                      引用 ['KB001'] 置信 0.95
[PASS] 02 知识库检索命中（发票）                      引用 ['KB005']
[PASS] 03 工具·查订单物流（规则路由）                 未接入 → 如实转人工 ✓
[PASS] 04 工具·发起退款                            未接入 → 如实转人工 ✓
[PASS] 05 工具·查退改政策                          命中 0 条政策
[PASS] 06 知识库未命中→自动转人工+工单                工单 T-cdaee537
[PASS] 07 注入护栏·block 拦截                      已拦截并记录
[PASS] 08 注入护栏·log_only 放行+留痕                放行业务 + 日志留痕
[PASS] 09 长期记忆·自动沉淀偏好                      沉淀 ['last_topic', 'pref_喜欢']
[PASS] 10 渠道会话·同群复用/跨群隔离                 同群复用 ✓ 跨群隔离 ✓
[PASS] 11 每身份限流（第 21 条 429）                 第 21 条触发 429，其他身份不受影响
[PASS] 12 坐席全流程（队列/接管/回复/关闭）             队列→接管→回复→关闭 ✓
[PASS] 13 反馈统计+差评自动升级                       好评率汇总 50%，差评自动升级 ✓
[PASS] 14 配置热更新（引擎模式即时生效）                改 rule 即刻生效并回滚 ✓
[PASS] 15 知识库 CRUD+新条目立即可检索               新增→检索命中→更新→删除 ✓
[PASS] 16 工具注册表 CRUD                         注册→列表→停用→删除 ✓
[PASS] 17 WS 实时推送（坐席端）                      坐席端实时收到 new_pending
合计 18 项：通过 18，失败 0
```

改造前同一条命令：**通过 11，失败 7**。

### 3.7 预设角色 API

```
$ curl -s -H "X-Admin-Token: $TOKEN" http://127.0.0.1:8010/api/personas
categories ['通用', '专业', '教育', '创意', '生活', '社群']
count 15
（zero/secretary/minimal/presale/tech/analyst/researcher/socratic/study-coach/
  interview-coach/creative/copywriter/schedule/companion/warm）
```

### 3.8 搜索与正文抽取

```
$ python -c "import asyncio; from core import websearch; \
  print(asyncio.run(websearch.search('AstrBot 是什么', 3)))"
首次 1.54s ok=True provider=bing
二次 0.14s ok=True provider=bing
（改造前：串行降级首次 25.6s）

$ python -c "from core.tools import _extract_main_text; print(_extract_main_text(html))"
'标题\n这是正文第一段，包含关键信息 42%。\n这是正文第二段，解释原因。'
（导航栏/Cookie 提示/页脚已剔除）
```

### 3.9 缓存头

```
$ curl -sI http://127.0.0.1:8010/assets/index-*.js | grep -i cache-control
cache-control: public, max-age=31536000, immutable

$ curl -sI http://127.0.0.1:8010/dashboard | grep -i cache-control
cache-control: no-cache, must-revalidate
```

## 四、Windows 本机注意事项

1. **必须用 Python 3.12**：3.14 下部分依赖（`sqlite-vec` 等）无 wheel。
2. **`uv venv` 默认不带 pip**：安装依赖用 `uv pip install --python .venv/Scripts/python.exe`。
3. **端口占用**：8010 被占时换端口（`--port 8011`），不要杀别人的进程。
4. **路径含空格**：`.env` 里 `CS_AGENT_STATIC_DIR` 含空格时不需要引号（dotenv 会保留）。
5. **未装可选依赖时的降级**：无 `sqlite-vec` → 纯 FTS5；无 `faster-whisper` → 语音转写明确报不可用；无 `edge-tts` → 发语音明确报未安装。都不会静默失败。

## 五、Linux 生产建议

```bash
# systemd 单元（示例）
[Unit]
Description=Zhike core
After=network-online.target

[Service]
WorkingDirectory=/opt/zhike
EnvironmentFile=/opt/zhike/.env
ExecStart=/opt/zhike/.venv/bin/python -m uvicorn core.main:app --host 127.0.0.1 --port 8010
Restart=always
User=zhike

[Install]
WantedBy=multi-user.target
```

反代（Nginx）关键项：

```nginx
location / {
    proxy_pass http://127.0.0.1:8010;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Request-ID $request_id;   # 关联 ID 透传
    proxy_buffering off;                          # SSE 必须关
    proxy_read_timeout 300s;
}
```

> `CS_AGENT_TRUSTED_PROXY=1` 只在反代本身可信时设置；否则本机令牌引导接口可被伪造来源利用。
