# Zhike · Your Personal Assistant

<div align="center">

[中文](README.md) · **English**

![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black)
![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Tests](https://img.shields.io/badge/tests-814%20passed-brightgreen)
![CI](https://github.com/badhope/zhike/actions/workflows/ci.yml/badge.svg)

</div>

---

**A self-hosted agent harness for personal assistants**: QQ group / private chat and Web channels, with a ReAct tool loop, native function calling, hybrid retrieval (FTS5 + vector RRF), long-term memory, reminder scheduling, tiered action permissions and end-to-end audit. Ships with a built-in catgirl persona "Zero" who is opinionated, deadpan and dodges politics. MIT licensed.

> Looking for the Chinese README? → [README.md](README.md)

## Why this project

Most "AI assistant" repos stop at a chat box. Zhike is the boring part done properly: tool permissions, approvals, audit chains, channel deduplication, loop guards, degraded modes and a test suite that actually runs in CI. It runs without any LLM key (rule + retrieval mode), and every optional backend hides itself instead of failing at call time.

## Design

| Component | Implementation | Standard |
|---|---|---|
| Tool calling | OpenAI function calling (`tools` + `tool_calls`), automatic JSON fallback | OpenAI API spec |
| Tool exposure | MCP server (`core/mcp_server.py`), callable by any MCP client | Model Context Protocol |
| Multi-step execution | ReAct loop PLAN → ACT → OBSERVE with step/tool/token/wall-clock/cost budgets and loop detection | ReAct + Agent SDK budgeting |
| Retrieval | SQLite FTS5 BM25 + optional BGE vectors, fused with RRF; lexical fallback on index failure | BM25 / RRF |
| Observability | Optional OpenTelemetry GenAI spans, metrics, structured event logs (default `logs/app.log`, 8MB rotation) | OTel GenAI conventions |
| Structured output | Pydantic validation with per-field lenient fallback | PydanticAI / Agents SDK |
| Permissions | L0 read / L1 reversible write / L2 destructive (forced approval) × autonomy T0–T3 | Least privilege + approval gate |
| Audit | SHA-256 hash chain over actions, approvals and config changes; verifiable in the console | Append-only audit chain |

## Features

- **Chat**: SSE streaming, image multimodal, Markdown, stop/regenerate, per-message delete
- **42 built-in tools** (35–40 exposed depending on configuration; unavailable backends hide instead of failing):
  image read / generate / send, voice reply (TTS), **voice input (local faster-whisper transcription)**,
  web search, page reader, document parsing (PDF/DOCX/XLSX), calculator, clock, long-term memory,
  reminders, group history / members / friends, proactive messaging, poke / emoji reaction / group
  essence / merged forward, and group administration (mute, card, rename, notice, upload, recall,
  join and friend approvals)
- **Bot-to-bot loop guard**: if the peer is also a bot (goodbye loops, echoes, question floods, instant
  ping-pong), the gateway goes silent for 30 minutes (`gateway/loop_guard.py`, on by default)
- **Reminders**: `in 10 minutes` / `tomorrow at 9` / `daily at 8` / `weekdays at 9`, scheduled in local
  time and delivered back to the originating channel
- **RAG knowledge base**: Markdown / PDF / Word / Excel import, chunking, retrieval test, canned answers
  (instant reply on hit)
- **Operator desk**: takeover queue, internal notes, canned replies, first-response SLA, one-click save
  to the knowledge base
- **Bilingual UI**: switch `中 / EN` in the sidebar; all interface chrome is translated, business data
  stays as-is
- **QQ, fully wired**: face / mface emoji become readable tokens, group replies quote the message,
  per-user and per-group profiles, auto-learned group slang, dedicated vision model
- **Built-in persona**: the catgirl "Zero" ships as a first-class default (`core/persona.py`), mirrored
  byte-for-byte by the console preset; override with `USER_PERSONA` or edit it in the UI
- **Channels**: QQ (NapCat + OneBot v11 + NoneBot2 gateway) and Web; adapter skeleton for WeCom /
  official accounts

## Mirrors

The same code is published to five public repositories (pick any, contents identical):

- GitHub: [badhope/zhike](https://github.com/badhope/zhike) · [X33834/zhike](https://github.com/X33834/zhike) · [Morningstar202604/zhike](https://github.com/Morningstar202604/zhike)
- Gitee: [badhope/zhike](https://gitee.com/badhope/zhike)
- GitCode: [badhope/zhike](https://gitcode.com/badhope/zhike)

Landing page: [badhope.github.io/zhike](https://badhope.github.io/zhike/)

## Quick start

```bash
git clone https://github.com/badhope/zhike && cd zhike
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Linux/macOS: .venv/bin/pip
cp .env.example .env                      # fill USER_LLM_* / ADMIN_TOKEN as needed

cd frontend && npm ci && npm run build && cd ..
CS_AGENT_STATIC_DIR=frontend/dist python -m uvicorn core.main:app --port 8010
# → http://localhost:8010  one address = console + API
```

Runs without an LLM key: the pipeline degrades to rule + retrieval answering.

QQ setup (NapCat, QR login, gateway) is in [docs/QQ-LOGIN.md](docs/QQ-LOGIN.md); Docker, reverse proxy
and free-tier deployment in [DEPLOY.md](DEPLOY.md).

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `USER_LLM_API_KEY/BASE_URL/MODEL` | OpenAI-compatible endpoint (DeepSeek / GLM / Qwen) | empty (rule mode) |
| `USER_PERSONA` | Override the built-in persona | empty (built-in "Zero") |
| `ADMIN_TOKEN` | Admin API token, **required in production** | empty (dev mode, warns) |
| `CHANNEL_TOKEN` | Shared channel secret for `/v1/ingest` and gateway heartbeat | empty (dev mode) |
| `AGENT_TIER` / `ALLOW_TIER3` | Agent autonomy 0–3; T3 needs both switches | 1 / off |
| `ACTION_L2_ENABLED` | Allow destructive actions (still needs approval) | false |
| `AUTHORIZED_QQ` | QQ ids allowed to use privileged tools (file access, proactive messaging) | empty |
| `CS_AGENT_STATIC_DIR` | Frontend dist for single-port mode | empty |

Full list in [.env.example](.env.example).

## Screenshots

| Overview | Conversations |
|---|---|
| ![Overview](docs/screenshots/dashboard.png) | ![Conversations](docs/screenshots/sessions.png) |

| Operator desk | Action audit |
|---|---|
| ![Operator desk](docs/screenshots/operator.png) | ![Action audit](docs/screenshots/actions.png) |

| Agent runs | Reminders |
|---|---|
| ![Agent runs](docs/screenshots/runs.png) | ![Reminders](docs/screenshots/reminders.png) |

| Persona | Knowledge base |
|---|---|
| ![Persona](docs/screenshots/persona.png) | ![Knowledge base](docs/screenshots/kb.png) |

| Visitor chat | Mobile |
|---|---|
| ![Visitor chat](docs/screenshots/visitor-chat.png) | ![Mobile](docs/screenshots/visitor-mobile.png) |

**English UI** (switch `中 / EN` in the sidebar):

| Dashboard (EN) | Conversations (EN) |
|---|---|
| ![Dashboard EN](docs/screenshots/en-dashboard.png) | ![Conversations EN](docs/screenshots/en-sessions.png) |

Screenshots are captured against a demo database from `scripts/seed_demo.py` using
`scripts/take_screenshots.py`.

## Tests

```bash
python -m pytest tests -q                      # 814 passed
ruff check core executor gateway scripts --select E9,F63,F7,F82,F401,F841
cd frontend && npx tsc --noEmit && npm test && npm run build
ADMIN_TOKEN=xxx CHANNEL_TOKEN=yyy python scripts/e2e_check.py http://127.0.0.1:8010
```

CI runs the same commands on Python 3.12 and Node 22, with a 70% coverage gate, `pip-audit` on
production dependencies and `npm audit --omit=dev --audit-level=high`.

## Security

- Deny by default: admin APIs return 503 without `ADMIN_TOKEN`; action execution requires an
  ADMIN / AGENT / CHANNEL token
- Visitor sessions use `sid + session_secret`; WS credentials travel via `Sec-WebSocket-Protocol`,
  never into access logs
- Injection guardrail, per-identity and per-IP rate limits, SPA path-traversal guard, security headers
  (CSP / X-Frame-Options / nosniff)
- LLM keys from env are never persisted; keys saved in the UI are Fernet-encrypted in SQLite and always
  masked on read
- Principal-aware tool layer: ordinary group members cannot read/write host files or send messages as
  the bot (`AUTHORIZED_QQ` allowlist)

## Layout

```
core/       FastAPI service: pipeline, tools, retrieval, memory, reminders, action authz, audit
gateway/    NoneBot2 + OneBot v11 gateway: QQ messages, human-like pacing, group throttling, loop guard
executor/   Standalone computer executor: path allowlist, command allowlist, trash-by-default delete
frontend/   React console (Vite + TS/JSX + ECharts + react-i18next)
tests/      814 pytest regressions
docs/       QQ setup, persona evaluation, screenshots, landing page
```

## Persona evaluation

Three gates keep the assistant sounding like a person instead of a model:

| Layer | Location | Purpose |
|---|---|---|
| Intent routing | `core/intent.py` | Rule-based routing for identity / emoji / provocation / politics / livelihood / tools / injection |
| Output discriminator | `core/quality.py` | Catches self-identification as AI, model leakage, capability denials, fake hand-offs, markdown and support-desk tone; one rewrite on severe issues |
| Multi-model selection | `core/ensemble.py` | Two candidates for casual intents, rule-based scoring picks the one with fewer defects |

Full numbers, replays and pitfalls: [docs/PERSONA-EVAL.md](docs/PERSONA-EVAL.md) (320-question A/B,
3000-question stress scan, 15 multi-turn scenarios, tool-selection suite, live QQ self-QA).

## Keywords

self-hosted, personal assistant, AI agent, agent harness, LLM agent, OpenAI function calling, MCP,
Model Context Protocol, ReAct, RAG, retrieval augmented generation, hybrid search, SQLite FTS5,
vector search, long-term memory, reminder scheduler, QQ bot, NoneBot2, OneBot v11, NapCat, chatbot,
FastAPI, React, TypeScript, bilingual UI, Chinese, observability, OpenTelemetry, audit log,
permission model, approval workflow, self-hostable, MIT license

## License

MIT. NapCat is an unofficial protocol client under a custom non-commercial license — review its
LICENSE and Tencent's risk-control policy before commercial use.
