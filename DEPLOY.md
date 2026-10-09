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

- [ ] `curl http://127.0.0.1:8010/healthz` 返回 `{"ok":true}`
- [ ] 控制台「运行总览」能打开，管理中心用 ADMIN_TOKEN 登录成功
- [ ] `docker logs zhike-gateway` 无报错；控制台渠道页 onebot_v11 显示 online
- [ ] QQ 群 @机器人 → 秒回知识库内容（带 KB 引用）
- [ ] 访客页（`/`）发消息 → 正常对话
- [ ] 未带令牌 `curl -X POST http://IP:8010/v1/ingest` → **401**（安全生效）

## 4. 日常运维

```bash
# 升级（拉新代码重建）
bash /opt/zhike/scripts/deploy.sh
# 日志
docker logs -f zhike-core / zhike-gateway / zhike-napcat
# 备份（SQLite 在 volume 里；热备用 sqlite3 .backup）
docker run --rm -v zhike_core_data:/data -v $PWD:/b alpine cp /data/cs_agent.db /b/
# 回滚：git -C /opt/zhike checkout <旧tag或sha> && bash scripts/deploy.sh
```

## 5. 安全基线（生产必做）

- `.env` 里 `ADMIN_TOKEN`、`CHANNEL_TOKEN` 均已随机化（脚本自动），不要改回占位符
- 不确定就收紧：`ZHIKE_BIND=127.0.0.1` + 反代/隧道对外
- NapCat WebUI（6099）只绑本机，走 SSH 隧道访问
- 公网直连场景建议 `CS_AGENT_CORS_ORIGINS` 填控制台实际域名

## 6. 常见问题

| 症状 | 处理 |
|---|---|
| gateway 一直连不上 NapCat | NapCat WebUI 网络配置里没开 WS 服务端 3001，或容器没在同一 compose 网络 |
| 机器人登录频繁掉线 | 换固定设备登录、别频繁重启容器；NapCat 登录态在 napcat_data volume 里持久化 |
| 控制台 401 | 管理中心输入 ADMIN_TOKEN；浏览器 localStorage 里是旧令牌就清掉重输 |
| LLM 不生效 | 管理中心「LLM 配置」看连通测试；`USER_LLM_API_KEY` 为空时是规则模式 |
