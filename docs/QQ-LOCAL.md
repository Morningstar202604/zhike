# 本机 QQ 接入（免扫码）：NapCat Shell + 快速登录

> 实测环境：Windows 11 + 桌面 QQNT（`C:\Program Files\Tencent\QQNT`）+ NapCat Shell v4.18.41
> 结论先行：**不用扫码是可行的**。NapCat 的快速登录列表能识别本机 QQ 数据目录里已有的登录凭据，
> 调用一次 `SetQuickLogin` 即可让机器人接管该账号；之后每次启动 `--qq <号>` 自动登录。
> 唯一约束：**同一账号的桌面 QQ 与机器人互斥**，机器人在线时桌面 QQ 必须退出（想用桌面版就先 `stop`）。

## 一、调研结论（2025-2026 各项目的做法）

| 方案 | 免扫码？ | 原理 | 风险 | 结论 |
|---|---|---|---|---|
| **NapCat Shell 快速登录**（本文采用） | ✅（首次之后永久） | 复用本机 NTQQ 数据目录里的登录凭据，`launcher.bat <QQ号>` 直接登录 | 与桌面 QQ 互斥；非官方协议的通用风控风险 | **推荐**：不修改 QQ 安装、不碰主号客户端文件 |
| NapCat Framework（LiteLoaderQQNT 注入） | ✅ 完全免扫 | 把插件装进桌面 QQ 客户端本体，用它现成的登录态 | 需修改 QQ 安装、QQ 升级易失效、主号直接暴露给注入框架 | 不自动做；想要的人自己按官方文档装 |
| LLOneBot（同为 LiteLoaderQQNT 插件） | ✅ 完全免扫 | 同上 | 同上 | 同上 |
| Lagrange.Core / go-cqhttp | ❌ | 独立协议实现，要自己的登录（密码+签名服务器/已停更） | go-cqhttp 已停更，密码登录历史协议封号风险最高 | 不采用 |
| QQ 官方机器人开放平台 | ✅ | 官方 API，无风控风险 | 功能受限（戳一戳/精华/群管理等高级动作没有），要注册审核 | 与知客工具集不匹配，不采用 |

风控要点（来自 NapCat 官方安全说明与社区实测）：

- 非官方协议存在账号被风控的可能（警告/冻结/封禁），**社区共识是别用主力日常号**。
- 你现在这个方案 = 用自己的号在同一台电脑上跑机器人。NapCat 官方建议 Bot 与常用账号分开 IP/设备——
  同账号同设备反而没有"两个环境"的问题，但账号本身的账号风险无法消除。
- 出现频繁掉线（`KickedOffline`）时：先重跑 `start --qq <号>`；反复发生则考虑换号。
- WebUI 默认端口/token 只绑 127.0.0.1，不要开放公网。

## 二、本机已完成的配置

以下都已就位（脚本自动完成，重装时按此检查）：

1. **NapCat Shell v4.18.41** 解压在 `.local/napcat/`（QQ 安装沿用系统里的 QQNT，不改 Program Files）。
2. **`config/webui.json`**：WebUI 固定 `127.0.0.1:6099`，令牌 `zhike-local-6099`（只本机可访问）。
3. **`config/onebot11.json`**（全局默认网络配置）：WebSocket 服务端 `127.0.0.1:3001`。
   NapCat 对没有专属配置的账号会**回退读这份全局配置**并自动另存为 `onebot11_<QQ号>.json`
   （从 v4.18.41 产物源码验证：`read()` 先找账号配置，缺省回落全局并落盘）——所以登录后无需在
   WebUI 手动点网络配置。
4. **`config/napcat.json`**：开启 fileLog（`logs/`），`o3HookMode` 保持默认 1。

## 三、日常使用（三条命令）

```bash
# 1. 启动机器人（快速登录，免扫码；桌面 QQ 会自动被顶掉）
.venv/Scripts/python.exe scripts/napcat_local.py start --qq <你的QQ号>

# 2. 启动核心 + 网关（core 已加载 .env；gateway 现在也会自动加载 .env）
.venv/Scripts/python.exe -m uvicorn core.main:app --host 127.0.0.1 --port 8010
ONEBOT_WS_URL=ws://127.0.0.1:3001 .venv/Scripts/python.exe gateway/bot.py

# 3. 查状态 / 停止
.venv/Scripts/python.exe scripts/napcat_local.py status
.venv/Scripts/python.exe scripts/napcat_local.py stop
```

验证接入是否成功：

```bash
curl -H "X-Admin-Token: <ADMIN_TOKEN>" http://127.0.0.1:8010/api/gateway/health
# {"online":true,"connected":true,"self_id":"<QQ号>","bots":1,...}
```

网关日志出现 `Bot <QQ号> connected` 即接入完成；之后别人在群里 @机器人 / 私聊它就会走知客管线。

## 四、首次登录如何做到零扫码（原理）

桌面 QQ 登录后，登录凭据持久化在本机 NTQQ 数据目录（`~/Documents/Tencent Files/<QQ号>/nt_qq`）。
NapCat Shell 启动的就是这份 QQ 客户端（`NapCatWinBootMain.exe` 以注入方式拉起系统里已装的 `QQ.exe`），
所以它能看到同一份凭据：

1. `GET /api/QQLogin/GetQuickLoginList` → 返回 `["<QQ号>"]`（凭据识别成功）。
2. `POST /api/QQLogin/SetQuickLogin {"uin":"<QQ号>"}` → 直接登录。
   ⚠️ 若桌面 QQ 正在运行同一账号，会报「当前账号已登录,无法重复登录」——先退出桌面 QQ。
3. 登录成功后 OneBot WS(3001) 立即可用；之后每次 `start --qq <号>` 都走这条路，**永不再扫码**。

只有在 NapCat 自己的凭据缓存被清（删 `.local/napcat/config` / 手机端踢设备 / 长期掉线）时才需要重新扫码。

## 五、排障

| 现象 | 处置 |
|---|---|
| `SetQuickLogin` 报「当前账号已登录」 | 桌面 QQ 正开着同一账号：先退出桌面 QQ（或 `taskkill /IM QQ.exe /F`）再点一次快速登录 |
| 3001 可连但握手无响应 | QQ 没登录完成；看 `.local/napcat/logs/` 与 `napcat_local.py status` |
| 网关反复 `ConnectionRefusedError` 后又 connected | NapCat 重启导致的正常重连（NoneBot 自动恢复，无需干预） |
| 频繁 `KickedOffline` | 手机 QQ「设置→账号安全→登录设备」确认本机为可信设备；仍频繁则换号 |
| 控制台中文乱码 | 显示层编码问题，不影响功能；`napcat_local.py` 已强制 UTF-8 输出 |

## 六、与 Docker 方案的关系

`docs/QQ-LOGIN.md` 的 Docker 路径仍然有效（适合服务器/不想装 QQ 的机器）。
本机路径的优势：省去 Docker、复用本机已登录凭据零扫码；代价是与桌面 QQ 互斥。
两者二选一即可，不要同时跑（同一账号两处登录会互踢）。
