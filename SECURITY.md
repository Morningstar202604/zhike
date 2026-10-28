# 安全策略 / Security Policy

## 报告漏洞

**不要**在公开 Issue 里披露漏洞细节。请通过 GitHub 的
[Private vulnerability reporting](https://github.com/badhope/zhike/security/advisories/new)
提交，或先开一个不含细节的 issue 说明「发现安全问题，请求私下沟通渠道」。

我们会在确认后修复，修复完成再公开披露。请给出：影响版本、复现步骤、影响面评估。

## 支持版本

只对 `main` 分支最新提交提供安全修复。

## 部署侧必读

- 生产必须设置 `ADMIN_TOKEN` 与 `CHANNEL_TOKEN`；两者为空时管理接口与 `/v1/ingest` 处于开发模式。
- 公网部署时设置 `CS_AGENT_LOCAL_TOKEN_BOOTSTRAP=0`，关闭本机自动下发令牌通道。
- 执行器默认只监听回环地址；改成 `0.0.0.0` 前必须设置 `EXECUTOR_TOKEN`。
- NapCat WebUI（6099）与 OneBot（3001）只绑 `127.0.0.1`，不要映射到公网。
- `AUTHORIZED_QQ` 只填自己的账号：它决定谁能触达读写文件、以机器人身份发消息这类特权工具。

## 仓库内的安全边界

- 本仓库不含任何密钥、令牌、真实账号或聊天记录；`.gitignore` 排除 `*.db`、`.env`、`logs/`、`out/`。
- 界面上保存的 LLM Key 使用 Fernet 加密存 SQLite，读取永远掩码；环境变量注入的 Key 不落库。
- 动作执行采用 L0 只读 / L1 可逆写 / L2 破坏性（强制审批）分级，并写 SHA-256 哈希链审计。
