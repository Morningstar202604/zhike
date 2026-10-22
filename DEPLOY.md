# 知客 Zhike 部署手册

## 0. 先回答：要不要 Cloudflare？

取决于**服务跑在哪、谁来访问**：

| 你的部署位置 | 需要什么 | 说明 |
|---|---|---|
| ☁️ 云 VPS（有公网 IP） | **什么都不用** | `http://IP:8010` 直接可用；想要域名/HTTPS 再加 Caddy/nginx |
| 🏠 家宅/NAS（无公网 IP），但想外网访问 | **CF Tunnel**（推荐，免费） | 需要：CF 账号 + 托管在 CF 的域名。建 Tunnel 拿到 token，`cloudflared` 指向 `http://127.0.0.1:8010` 即可，无需开放任何入站端口 |
| 🏠 家宅/NAS，只在局域网用 | **什么都不用** | 家里打开 `http://NAS-IP:8010` |
| 🔒 两者都只想自己用 | Tailscale/frp 二选一 | 装好 Tailscale 后访问 `http://100.x.x.x:8010` |

> 结论：**CF 不是必选项**。只有"家宅部署 + 外网访问"这一种情况才需要它。

## 0.5 零成本公益部署（一分钱不花）

QQ 机器人需要 7×24 在线，先说清楚物理规律：**"完全免费 + 不用自己任何设备 + 稳定不掉线"三者在免费平台上不可兼得**。按推荐度排序：

### 路径 A：家里任何一台能开机的设备（最推荐，真·零成本）

旧电脑 / NAS / 旧安卓手机都可以：

- **电脑/NAS**：装 Docker → 跑一键脚本，同主手册
- **旧安卓手机（进阶）**：Termux 装 Python/Node → `git clone` → 分别跑 core、gateway 与 NapCat（社区有 NapCat-Termux 教程）。旧手机插电息屏就是一台 7×24 低功耗服务器，QQ 登录还更稳（家宽+移动设备 IP，腾讯风控友好）
- 局域网外访问：Tailscale（免费 3 用户）装手机+设备各一，随时随地连

### 路径 B：Oracle Cloud 永久免费层（要一张信用卡做验证，不扣费）

- Always Free：4 核 ARM + 24G 内存，额度永久，跑本项目只用零头
- 注意：注册有门槛（信用卡验证 + 区域库存），闲置实例可能被回收；回收后重建+重扫 QQ 即可
- 跑法同主手册（Docker 装好 → `bash scripts/deploy.sh`）

### 路径 C：Hugging Face Spaces 容器（无需信用卡，尽力而为）

- 免费真金：2 vCPU / 16G 内存跑 Docker
- **硬伤**：文件系统临时（QQ 登录态重启丢失→需重扫）、免费档会休眠（首条消息慢）、数据中心 IP 易触发腾讯风控
- 结论：适合验证玩法，不适合当 7×24 生产

### 免费 LLM：智谱 GLM-4-Flash（真免费，国内直连）

```env
USER_LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
USER_LLM_MODEL=glm-4-flash
USER_LLM_API_KEY=（open.bigmodel.cn 注册即送，Flash 档 0 元调用）
# 可选：看图（多模态）。配了视觉模型名，用户发图就走 LLM 看图，不配则图片转人工
USER_VISION_MODEL=glm-4v-flash
```

不配 Key 也行：规则+检索模式永远可用（免费路径的兜底）。

### 可选增强（都不影响默认可用性）

```bash
# 向量语义检索（BGE 中文小模型，CPU 可跑；1G 内存小机别装，用默认字面检索）
pip install -r requirements-embedding.txt
```

环境变量：`USER_VISION_MODEL`（看图，如 `glm-4v-flash`）；检索开关在管理配置 `retrieval.embedding`（auto/off）。

## 1. 准备清单

| 项目 | 必要性 | 说明 |
|---|---|---|
| 部署机 | ✅ 必须 | 1 核 1G 起步即可（很轻）；装好 Docker（`curl -fsSL https://get.docker.com \| sh`） |
| 机器人 QQ 号 | ✅ 必须（QQ 渠道） | 建议小号；**首次启动要扫码登录，这一步只能在部署机上做**（腾讯风控） |
| LLM Key | ⭕ 可选 | DeepSeek/GLM/Qwen 任一 OpenAI 兼容 Key；不填=规则+检索模式，照样能跑 |
| 域名 | ⭕ 可选 | 想要 HTTPS/好记的地址才需要 |

## 2. 五步上线（云 VPS）

```bash
# ① 下载并执行一键脚本（首次会生成 .env 并自动随机化两把令牌）
bash <(curl -fsSL https://gitee.com/raw/badhope/zhike/master/scripts/deploy.sh) \
  || git clone --depth 1 https://gitcode.com/badhope/zhike.git /opt/zhike && bash /opt/zhike/scripts/deploy.sh

# ② 编辑 /opt/zhike/.env：填 NAPCAT_QQ（机器人 QQ 号）、按需填 USER_LLM_API_KEY
vim /opt/zhike/.env

# ③ 重跑脚本 → 构建镜像并启动（core 自带控制台，单端口 8010）
bash /opt/zhike/scripts/deploy.sh

# ④ QQ 扫码登录：本机浏览器访问 NapCat WebUI
ssh -L 6099:127.0.0.1:6099 user@服务器IP   # 然后浏览器开 http://127.0.0.1:6099
#    WebUI 令牌：docker logs zhike-napcat | grep token
#    登录机器人 QQ 后，在网络配置里添加「WebSocket 服务端」监听 0.0.0.0:3001（一次性，持久化）

# ⑤ 验收
curl http://127.0.0.1:8010/healthz            # {"ok":true}
# 控制台 http://服务器IP:8010 → 管理中心 → 用 ADMIN_TOKEN 登录
# QQ 群里 @机器人 问一句「订单什么时候发货」→ 应收到带引用的回复
```

家宅 + CF Tunnel 变体：`ZHIKE_BIND=127.0.0.1` 写进 .env（core 不暴露公网），`cloudflared tunnel` 指向 `http://127.0.0.1:8010`。

## 3. 验收 Checklist

### 3.1 机器可自动验收（无需登录 QQ，5 分钟）

```bash
.venv/bin/python scripts/qq_e2e.py
```

模拟 NapCat 走通「OneBot v11 → 网关 → 核心 → 回复」全链路，输出：

```
✅ 私聊 KB 问答
✅ 群聊 @+图片→转人工
✅ 私聊转人工→升级话术
结果：QQ 渠道全链路全部通过 ✅
```

排障日志：`/tmp/zhike_qq_gw.log`、`/tmp/zhike_qq_core.log`。

### 3.2 真机验收（唯一人工环节：小号扫码）

1. 本机浏览器打开 NapCat WebUI（`http://部署机IP:6099`，默认 token 见 compose 输出）
2. 用**机器人小号**扫码登录，等 NapCat 显示「在线」
3. 大号加机器人好友，逐条验收：
   - [ ] 私聊「退货政策是什么」→ 秒回知识库答案
   - [ ] 私聊发一张截图 → 回复"转接人工"（配了 `USER_VISION_MODEL` 则为看图回答）
   - [ ] 拉进群 @机器人 问一句 → 群里正常回复（没 @ 不理人）
   - [ ] 群里只发图 @机器人 → 回复转人工
   - [ ] 说「转人工」→ 升级话术 + 控制台出现待处理工单
4. 控制台「会话/工单」能看到上述记录

### 3.3 基础项

- [ ] `curl http://127.0.0.1:8010/healthz` 返回 `{"ok":true}`
- [ ] 控制台「运行总览」能打开，管理中心用 ADMIN_TOKEN 登录成功
- [ ] `docker logs zhike-gateway` 无报错；控制台渠道页 onebot_v11 显示 online
- [ ] 访客页（`/`）发消息 → 正常对话（LLM 配好时打字机逐字出）
- [ ] 未带令牌 `curl -X POST http://IP:8010/v1/ingest` → **401**（安全生效）

## 4. 日常运维

```bash
# 升级（拉新代码重建）
bash /opt/zhike/scripts/deploy.sh
# 日志
docker logs -f zhike-core / zhike-gateway / zhike-napcat
# 备份（SQLite 在 volume 里）
# 必须用 sqlite3 .backup：WAL 模式下直接 cp 可能拿到「主库已写、WAL 未合并」的不一致快照。
docker run --rm -v zhike_core_data:/data -v "$PWD":/b alpine sh -c   'apk add --no-cache sqlite >/dev/null && sqlite3 /data/cs_agent.db ".backup /b/backup.db"'
# 回滚：git -C /opt/zhike checkout <旧tag或sha> && bash scripts/deploy.sh
```

**恢复演练（每季度至少一次，脚本化）**

```bash
# 1) 停服务，保留现场
docker compose -f deploy/docker-compose.yml stop core
# 2) 用备份替换（同样走 sqlite3，不要直接覆盖正在使用的文件）
docker run --rm -v zhike_core_data:/data -v "$PWD":/b alpine sh -c   'apk add --no-cache sqlite >/dev/null && sqlite3 /b/backup.db ".backup /data/cs_agent.db"'
# 3) 起服务并校验
docker compose -f deploy/docker-compose.yml start core
curl -s localhost:8010/healthz
ADMIN_TOKEN=... curl -s -H "X-Admin-Token: $ADMIN_TOKEN" localhost:8010/api/admin/audit/verify
#    → ok=true 表示哈希链完整；再抽样对比 /api/admin/overview 的 sessions/messages 数量
```

## 5. 可观测性

| 端点 | 用途 |
|---|---|
| `GET /healthz` | 存活探针（容器 healthcheck 用） |
| `GET /metrics` | Prometheus 文本格式：`zhike_http_requests_total`（按路由模板/状态码）、`zhike_http_request_duration_seconds_sum|max`、`zhike_llm_calls_total{result=ok|fail|fallback}`、`zhike_uptime_seconds` |
| `GET /api/admin/logs` | 最近事件（控制台「日志中心」） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 可选：OpenTelemetry trace 导出（需装 `requirements-tracing.txt`） |

**建议的三条告警（按用户可见症状）**

| 告警 | 表达式（示例） | 含义 / 处置 |
|---|---|---|
| 5xx 比例 | `sum(rate(zhike_http_requests_total{status=~"5.."}[5m])) / sum(rate(zhike_http_requests_total[5m])) > 0.02` | 服务端批量报错：先看 `/api/admin/logs?level=ERROR`，再查 LLM 端点连通性 |
| LLM 失败率 | `rate(zhike_llm_calls_total{result="fail"}[5m]) / ignoring(result) sum(rate(zhike_llm_calls_total[5m])) > 0.2` | 模型不可用：系统会自动降级规则模式，检查 Key/额度/网络 |
| 接口 P95 劣化 | `max by (path) (zhike_http_request_duration_seconds_max) > 2` | 单请求超 2s：查 SQLite 锁等待（`rate_hits`/审计链写）与检索索引重建 |

## 6. 安全基线（生产必做）

- `.env` 里 `ADMIN_TOKEN`、`CHANNEL_TOKEN` 均已随机化（脚本自动），不要改回占位符
- 不确定就收紧：`ZHIKE_BIND=127.0.0.1` + 反代/隧道对外
- NapCat WebUI（6099）只绑本机，走 SSH 隧道访问
- 公网直连场景建议 `CS_AGENT_CORS_ORIGINS` 填控制台实际域名

## 7. 常见问题

| 症状 | 处理 |
|---|---|
| gateway 一直连不上 NapCat | NapCat WebUI 网络配置里没开 WS 服务端 3001，或容器没在同一 compose 网络 |
| 机器人登录频繁掉线 | 换固定设备登录、别频繁重启容器；NapCat 登录态在 napcat_data volume 里持久化 |
| 控制台 401 | 管理中心输入 ADMIN_TOKEN；浏览器 localStorage 里是旧令牌就清掉重输 |
| LLM 不生效 | 管理中心「LLM 配置」看连通测试；`USER_LLM_API_KEY` 为空时是规则模式 |
