# Zhike

[中文](README.md)

Zhike is a self-hosted personal-assistant agent foundation for QQ and the Web. It combines conversation, tool calling, knowledge retrieval, memory and reminders with action permissions, approvals and audit records.

## Capabilities

- **QQ and Web conversations:** Web sessions and QQ group or private chat. QQ requires a separately deployed and authenticated NapCat / OneBot v11 gateway.
- **Agents and tools:** multi-step tool calls, execution budgets and repeated-action checks. Availability depends on configuration, permissions and external services; unimplemented custom tools explicitly report that they are not connected.
- **Knowledge and memory:** knowledge-base import and retrieval, plus long-term memory. Basic retrieval uses SQLite FTS5; vector retrieval is optional.
- **Reminders:** Chinese relative-time and recurring expressions such as `十分钟后` (in ten minutes) and `每天八点` (daily at eight). Delivery depends on the scheduler and channel connection.
- **Security and audit:** administrator tokens, tiered action permissions, approvals and audit records. Deployment and configuration still require care; see [SECURITY.md](SECURITY.md).
- **Personas and console:** built-in persona presets and a Web console for sessions, knowledge, reminders and run history.

## Quick start

Requires Python 3.12+ and Node.js. These commands use Git Bash; on Linux/macOS replace `.venv/Scripts/python` with `.venv/bin/python`.

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

Open <http://127.0.0.1:8010>. Configure an administrator token as described in `.env.example`; the QQ channel also needs a channel token. Without an LLM, rule and retrieval capabilities remain available. In automatic mode the QQ gateway may stay silent when the model is unavailable; explicit rule mode enables rule-based replies.

- QQ setup: [docs/QQ-LOGIN.md](docs/QQ-LOGIN.md)
- Deployment and reverse proxy: [DEPLOY.md](DEPLOY.md)
- Environment variables: [.env.example](.env.example)
- Architecture and boundaries: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Requirements and limitations

LLM, search, QQ messaging, vision, voice and business-system functions depend on their respective services, network, models, credentials and permissions. File operations also need the separate executor and an appropriate policy. WeCom and official-account integrations are adapter skeletons, not ready-to-use production integrations.

Natural-language reminder interpretation and delivery depend on the entry point, server time zone and runtime environment; verify time and time-zone behavior in the target deployment. Agent token and cost budgets are checked around calls, not enforced as strict limits on a single model request.

`/healthz` is a liveness check. `/readyz` checks database and knowledge-base readiness; its LLM and search indicators primarily report configuration and do not prove external service availability.

## Tests

After installing development dependencies, run from the repository root:

```bash
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m ruff check core executor gateway scripts tests
.venv/Scripts/python -m pytest tests -q
cd frontend
npm test
npm run build
```

Review skipped tests against the target environment. `scripts/e2e_check.py` creates sessions, messages and test data and performs writes and deletes. **Run it only against an isolated test instance**, not a production database or real QQ group.

## License

The project is licensed under [MIT](LICENSE). NapCat is a separate unofficial protocol implementation; review its license and relevant platform rules before use or commercial deployment.
