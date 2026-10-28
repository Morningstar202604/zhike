# 贡献指南 / Contributing

感谢你关注 **知客 Zhike**。Issue、文档、Bug 修复、新功能都欢迎。

## 开发环境

```bash
git clone https://github.com/badhope/zhike && cd zhike
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt      # Linux/macOS: .venv/bin/pip
cd frontend && npm ci && cd ..
```

## 提交前必须通过

```bash
python -m pytest tests -q
ruff check core executor gateway scripts --select E9,F63,F7,F82,F401,F841
cd frontend && npx tsc --noEmit && npm test && npm run build
```

CI 会跑同样的命令，另加 70% 覆盖率门槛、`pip-audit`（生产依赖）与 `npm audit --omit=dev --audit-level=high`。

## 提交规范

- Conventional Commits：`feat:` / `fix:` / `docs:` / `test:` / `chore:` / `refactor:`
- 一个提交只做一件事，方便回溯与回滚
- 提交信息说明「改了什么、为什么」，不要只写「update」
- **不要把密钥、令牌、真实 QQ 号/群号、聊天记录、数据库文件提交进仓库**

## 新增工具 / 渠道的约定

1. 工具必须在 `core/tools.py` 注册 `TOOL_META`（名称、描述、参数），并在 `tests/` 补一条真实调用测试。
2. 需要外部配置的工具写进 `_REQUIRES_CONFIG`：没配置时从工具池隐藏，而不是让模型调用后报错。
3. 涉及权限的动作走 `core/actions/registry.py`，标明风险级别；破坏性动作必须进入审批流。
4. 渠道适配器放 `gateway/adapters/`，保持 `NormalizedMessage` 字段语义不变。

## 问题反馈

- 先搜索是否已有同类 issue
- Bug 请附：复现步骤、期望/实际行为、Python 与 Node 版本、平台
- 安全漏洞请按 [SECURITY.md](SECURITY.md) 私下报告，不要开公开 issue

## 行为准则

参与贡献即表示同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
