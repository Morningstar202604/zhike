# 知客 Zhike

[English](README.en.md)

知客是一个可自托管的 QQ / Web 私人秘书 Agent 底座。它将对话、工具调用、知识检索、记忆与提醒组合在一起，并提供动作权限、审批和审计能力。

## 主要能力

- **QQ 与 Web 对话**：Web 会话及 QQ 群聊、私聊；QQ 需要另行部署并登录 NapCat / OneBot v11 网关。
- **Agent 与工具**：多步工具调用、执行预算和重复动作检查。具体工具的可用性取决于配置、权限和外部服务；未实现的自定义工具会明确返回未接入。
- **知识与记忆**：知识库导入和检索、长期记忆；基础检索使用 SQLite FTS5，可选向量检索。
- **提醒**：支持中文相对时间和周期表达，例如“十分钟后”“每天八点”；具体投递依赖渠道连接与调度器。
- **安全与审计**：管理接口令牌、动作权限分级、审批和审计记录。配置和部署不当仍可能造成风险，请遵循 [安全说明](SECURITY.md)。
- **角色与控制台**：内置多种预设角色，可通过控制台管理；Web 控制台提供会话、知识库、提醒与运行记录入口。

## 快速开始

需要 Python 3.12+ 和 Node.js。以下命令使用 Git Bash；Linux/macOS 请将 `.venv/Scripts/python` 换成 `.venv/bin/python`。

```bash
git clone https://gitcode.com/badhope/zhike.git
cd zhike
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
cp .env.example .env

cd frontend
npm ci
npm run build
cd ..
CS_AGENT_STATIC_DIR=frontend/dist .venv/Scripts/python -m uvicorn core.main:app --host 127.0.0.1 --port 8010
```

打开 <http://127.0.0.1:8010>。按 `.env.example` 配置管理令牌；QQ 渠道还需要渠道令牌。模型未配置时可使用规则与检索能力；QQ 在自动模式且模型不可用时可能按策略静默，显式规则模式可使用规则回复。

- QQ 接入：[docs/QQ-LOGIN.md](docs/QQ-LOGIN.md)
- 部署与反向代理：[DEPLOY.md](DEPLOY.md)
- 环境变量：[.env.example](.env.example)
- 架构与边界：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## 运行条件与限制

LLM、联网搜索、QQ 消息收发、视觉、语音和业务系统功能依赖对应服务、网络、模型、凭据及权限，不保证开箱即用。文件操作还需要独立执行器及其策略配置。企业微信和公众号目前是适配骨架，不是现成的生产接入。

提醒的中文自然语言解析和渠道投递取决于入口、服务器时区及运行环境；使用前请在目标环境验证时间和时区。Agent 的 token 与费用预算在调用前后统计，并非单次模型请求的严格硬上限。

`/healthz` 用于存活检查；`/readyz` 检查数据库、知识库等就绪条件，LLM 和搜索项主要反映配置状态，并不证明外部服务可用。

## 测试

安装开发依赖后，在仓库根目录运行：

```bash
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m ruff check core executor gateway scripts tests
.venv/Scripts/python -m pytest tests -q
cd frontend
npm test
npm run build
```

测试中的跳过项需结合环境判断。`scripts/e2e_check.py` 会创建会话、消息及测试数据，并执行写入或删除操作；**只对隔离测试实例运行**，不要直接指向生产数据库或真实 QQ 群。

## 许可

项目采用 [MIT 许可](LICENSE)。NapCat 是独立的非官方协议实现，使用及商用前请查阅其许可和相关平台规则。
