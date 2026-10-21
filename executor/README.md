# 执行器（Executor）

受策略约束的电脑操作进程。core 通过 HTTP 调用，**core 进程内不执行任何本地命令**。

## 为什么独立进程

1. 权限隔离：执行器可用标准用户运行，core 不必有系统权限
2. 崩溃隔离：执行器挂了不影响对话服务
3. 审计边界清晰：所有电脑操作都从一个进程出去，好审计

## 启动

```bash
cp executor/policy.example.json policy.json
# 编辑 policy.json，把 allow_roots 改成你真正允许操作的目录
EXECUTOR_POLICY_PATH=./policy.json python -m executor.main
```

默认监听 `127.0.0.1:8020`。**不要绑 0.0.0.0**：没设 `EXECUTOR_TOKEN` 时进程会拒绝启动。

## 安全模型

| 层 | 机制 |
|---|---|
| 网络 | 仅回环；非回环 + 无 token → 拒绝启动 |
| 鉴权 | HMAC-SHA256(token, ts + body) + 5 分钟时间戳窗口 |
| 路径 | allow_roots + deny_globs + 拒绝符号链接 + 拒绝 ADS/保留名 |
| 命令 | argv 数组白名单 + 参数模式拒绝 + `shell=False` + 超时杀进程 |
| 环境 | 子进程只拿到白名单变量，`*_TOKEN/*_KEY/*_SECRET` 一律剔除 |
| 删除 | 默认进回收站，可恢复 |
| 上限 | 读 1MB / 写 10MB / 输出 256KB / 列目录 2000 项 |

## 能力清单

| 能力 | 风险 | 说明 |
|---|---|---|
| `fs.list` / `fs.read` / `fs.stat` / `proc.list` | L0 只读 | Agent 可自主调用 |
| `fs.write` / `fs.mkdir` / `fs.move` / `shell.exec` | L1 可逆写 | 按档位放行 |
| `fs.delete` / `proc.kill` | L2 破坏性 | 永远需要人工确认 |

## 故障排查

- `策略拒绝：路径不在允许范围内` → `policy.json` 的 `allow_roots` 没包含该路径
- `策略拒绝：命令不在白名单` → 把可执行文件名加进 `shell.allow_binaries`
- `执行器不可达` → 进程没起，或 `EXECUTOR_URL` 不对
- `签名不匹配` → `EXECUTOR_TOKEN` 在 core 与执行器两边不一致
