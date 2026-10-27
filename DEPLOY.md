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
# 可选：看图（多模态）。配了视觉模型名，用户发图就走 LLM 看图，不配则请用户改用文字描述
USER_VISION_MODEL=glm-4v-flash
```

不配 Key 也行：规则+检索模式永远可用（免费路径的兜底）。

### 可选增强（都不影响默认可用性）

```bash
# 向量语义检索（BGE 中文小模型，CPU 可跑；1G 内存小机别装，用默认字面检索）
pip install -r requirements-embedding.txt
# 语音回复（send_voice 工具，edge-tts 走微软在线语音，约 1s 合成一句）
pip install -r requirements-voice.txt
# 模型需先在本机缓存（或把 USER_EMB_MODEL 指向已下载的目录）。
# 默认不联网下载：HF 默认重试会让首次检索阻塞 80 秒以上，需要下载时临时设 USER_EMB_ALLOW_DOWNLOAD=1。
```

环境变量：`USER_VISION_MODEL`（看图，如 `glm-4v-flash`；留空自动沿用主模型）、`USER_IMAGE_MODEL`（生图，默认 `agnes-image-2.1-flash`）；检索开关在管理配置 `retrieval.embedding`（auto/off）。

模型来源二选一：
- Hub 名称（默认 `BAAI/bge-small-zh-v1.5`）：首次需联网下载，之后走本地缓存。
- 本地目录（`USER_EMB_MODEL=/path/to/bge-small-zh-v1.5`，推荐离线部署）：代码自动置 `HF_HUB_OFFLINE=1`，不触网；也可显式设该变量，缺网时立即降级为字面检索，而不是卡在 Hub 重试上。

### 语音输入（收 QQ 语音消息）

QQ 语音是 AMR 格式，whisper 解不了，链路是「ffmpeg 转 16k PCM → faster-whisper 本地转写」。

```bash
# 1) 装依赖（可选；不装时收到语音会明确回「语音听不了，打字吧」）
pip install -r requirements-asr.txt
# 2) 装 ffmpeg（Windows 可用 winget install ffmpeg；Linux apt install ffmpeg）
# 3) 下载模型（约 460MB，只需一次）
HF_ENDPOINT=https://hf-mirror.com python -c "from faster_whisper import WhisperModel;   WhisperModel('small', download_root='C:/Users/YOU/models/whisper-small-zh')"
# 4) 把 ASR_MODEL 指向 snapshots 下的具体目录（含 model.bin 的那层）
```

实测数据（本机 CPU，16 核 / int8 / 4 线程）：

| 环节 | 耗时 |
|---|---|
| 冷启动加载模型 | 2.8s |
| 8 秒语音转写（热） | 3.0s |

热词：`core/asr.py` 的 `initial_prompt` 里带了「零、本喵、喵～、提醒」，否则 small 模型会把「零」听成「林」。

也支持外部命令：`ASR_CMD="python my_asr.py {wav}"`，stdout 即转写文本。

### 秘书工具集（默认全开，缺后端会诚实说明）

| 工具 | 作用 | 依赖 |
|---|---|---|
| `current_context` / `recall_memory` / `remember` | 读当前对话、读写长期记忆 | 无 |
| `remind` / `list_reminders` / `cancel_reminder` | 设提醒、查待办、取消（到点自动推回原渠道） | 无 |
| `read_webpage` / `web_search` | 读链接正文、联网搜索 | `SEARCH_PROVIDER` 等 |
| `list_files` / `read_file` | 读本机文件 | **执行器进程** + `EXECUTOR_POLICY_PATH` |
| `describe_image` | 看图/读图中文字 | LLM Key（未配 `USER_VISION_MODEL` 时自动沿用主模型） |
| `generate_image` / `send_image` | 文生图并发出、把图片发到当前会话 | LLM Key（`USER_IMAGE_MODEL`）/ 网关在线 |
| `parse_document` | 解析 PDF/DOCX/XLSX/MD/TXT | 无（本机走执行器，链接走公网抓取） |
| `now` / `calc` | 当前时间、数学计算 | 无 |
| `search_emote` | 搜收藏的表情包（视觉标签来自同目录 `tags.json`） | NapCat 表情目录（`GATEWAY_EMOTE_DIR`） |
| `send_voice` | 把一段话合成语音真发到会话 | `pip install -r requirements-voice.txt`（edge-tts）+ 网关在线 |
| `qq_group_history` / `qq_group_members` / `qq_groups` / `qq_friends` / `qq_send` | 读群记录、查成员/群/好友、主动发消息 | 网关在线 |
| `qq_poke` / `qq_emoji_like` / `qq_essence` / `qq_forward` | 戳一戳、表情回应、群精华、合并转发 | 网关在线（动作层判权限） |

安全边界：
- `read_webpage` 只允许公网 http/https，内网/回环/元数据地址在**建连之前**就被拒绝（`core/url_guard.py`）。
- `read_file` / `list_files` 受执行器 `allow_roots` + `deny_globs` 双重限制，越权直接拒绝并如实告知。
- 提醒投递失败会重试（默认 5 次）并记录 `last_error`，不会静默丢失。

启用执行器（文件工具）：

```bash
EXECUTOR_POLICY_PATH=/path/to/executor_policy.json EXECUTOR_BIND=127.0.0.1 EXECUTOR_PORT=8020 python -m executor.main
```

`executor_policy.json` 模板见 `executor/policy.example.json`；`allow_roots` 留空 = 全部拒绝（安全默认）。

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
# 控制台 http://服务器IP:8010 → 本机打开会自动登录；远程访问用管理中心「访问令牌」里复制的令牌
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
✅ 群聊 @+图片→请用户改用文字描述
✅ 私聊「转人工」→ 说明没有人工客服、机器人继续服务
结果：QQ 渠道全链路全部通过 ✅
```

排障日志：`/tmp/zhike_qq_gw.log`、`/tmp/zhike_qq_core.log`。

### 3.2 真机验收（唯一人工环节：小号扫码）

1. 本机浏览器打开 NapCat WebUI（`http://部署机IP:6099`，默认 token 见 compose 输出）
2. 用**机器人小号**扫码登录，等 NapCat 显示「在线」
3. 大号加机器人好友，逐条验收：
   - [ ] 私聊「退货政策是什么」→ 秒回知识库答案
   - [ ] 私聊发一张截图 → 回复"看不了图，请用文字描述"（配了 `USER_VISION_MODEL` 则为看图回答）
   - [ ] 拉进群 @机器人 问一句 → 群里正常回复（没 @ 不理人）
   - [ ] 群里只发图 @机器人 → 回复请用文字描述
   - [ ] 说「转人工」→ 明确告知只有机器人，会话不进入待接管
4. 控制台「会话/工单」能看到上述记录

### 3.3 基础项

- [ ] `curl http://127.0.0.1:8010/healthz` 返回 `{"ok":true}`
- [ ] 控制台「运行总览」能打开（本机自动登录；远程用「访问令牌」复制的令牌登录）
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
- 公网部署务必设 `CS_AGENT_LOCAL_TOKEN_BOOTSTRAP=0`：它会关闭「本机自动下发令牌」通道（默认只放行回环地址）
- 不确定就收紧：`ZHIKE_BIND=127.0.0.1` + 反代/隧道对外
- NapCat WebUI（6099）只绑本机，走 SSH 隧道访问
- 公网直连场景建议 `CS_AGENT_CORS_ORIGINS` 填控制台实际域名

## 7. 常见问题

| 症状 | 处理 |
|---|---|
| gateway 一直连不上 NapCat | NapCat WebUI 网络配置里没开 WS 服务端 3001，或容器没在同一 compose 网络 |
| 机器人登录频繁掉线 | 换固定设备登录、别频繁重启容器；NapCat 登录态在 napcat_data volume 里持久化 |
| 控制台 401 | 本机应自动登录；远程访问到「管理中心 → 访问令牌」复制令牌粘贴；仍是旧令牌就清 localStorage |
| LLM 不生效 | 管理中心「LLM 配置」看连通测试；`USER_LLM_API_KEY` 为空时是规则模式 |
