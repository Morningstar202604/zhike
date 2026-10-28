## 改了什么

<!-- 一句话说明，关联 issue 用 #123 -->

## 为什么

## 怎么验证的

- [ ] `python -m pytest tests -q`
- [ ] `ruff check core executor gateway scripts --select E9,F63,F7,F82,F401,F841`
- [ ] `cd frontend && npm run build && npm test`

## 检查项

- [ ] 没有提交密钥、令牌、真实 QQ 号/群号、聊天记录或数据库文件
- [ ] 新增/变更的行为有测试覆盖
- [ ] 文档已同步（README / DEPLOY / docs）
