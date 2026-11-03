# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versioning rules used here:

- **MAJOR** — incompatible changes: removed or renamed configuration keys, breaking API changes,
  database schema that cannot be migrated automatically.
- **MINOR** — backward-compatible features.
- **PATCH** — backward-compatible bug fixes, performance work, dependency and documentation updates.

The authoritative version string lives in [`VERSION`](VERSION), mirrored by
`core/version.py` and `pyproject.toml`. Run `python scripts/check_version.py` to verify.

---

## [Unreleased]

### Planned

- Split the remaining monoliths (`core/store.py`, `core/tools.py`, `core/main.py`) along entity
  boundaries: sessions, memory, actions, and the remaining admin routes.
- Raise frontend i18n coverage from ~36% to full parity.
- Promote `mypy` from advisory to blocking for newly added modules.

---

## [1.3.0] — 2026-11-03

Character cards: every persona preset ships with a structured, maintainable fact sheet instead of
a single blob of prompt text, and replies are checked against it before they leave the pipeline.

### Added

- **Character card system** (`core/character.py`). A persona is now two layers: structured
  *canon* (fields, narrative sections, voice samples, taboos, unknown-topic policy) stored as
  `core/data/characters/<id>.json`, and a fixed-order *renderer* that assembles the system
  prompt. All 15 built-in presets have cards. Cards with the same fact stated once stop the
  model from inventing an age, an address or a backstory mid-conversation.
- **Character consistency guard** (`core/character_guard.py`) wired into the reply pipeline.
  After the generic quality gate, the active persona's card is looked up by rendering-equality
  (`core/character.card_for_persona`) and the reply is checked for assistant-speak, identity
  drift, taboo phrases and lost catchphrases. Violations trigger one rewrite; if the rewrite
  fails on card-only issues the original text is kept rather than a fallback line in someone
  else's voice. Personas edited away from any card render are skipped — never judged against a
  card that does not match.
- `/api/personas` now reports `has_card` and a `card_summary` per preset; `GET
  /api/personas/{id}/card` returns the structured card. The console persona page shows a
  「设定」 badge on presets that have a card and opens a viewer with facts, narrative sections,
  voice samples and boundaries.
- `scripts/character_harness.py` — offline character regression (static mode is CI-safe, no
  model calls): every preset must render completely, pass drift checks, and stay distinct from
  the other presets (similarity floor 0.72).
- `tests/test_character_guard.py`, `tests/test_tools_uncovered.py` — card rendering, drift
  detection, pipeline integration, plus coverage for four previously untested tools
  (`initiate_refund`, `list_files`, `qq_friends`, `qq_groups`).

### Fixed

- **Identity rewrite criteria were hard-coded to the cat persona.** The gate demanded the word
  「猫」 in any reply to "who are you", so switching to 老陈 (tech support) or 知客 (secretary)
  forced a pointless rewrite of every correct answer. Anchors are now inferred from the persona
  itself (`quality._identity_anchors`), and rewrite prompts no longer re-assert the cat persona.
- **Fallback lines spoke in the wrong character's voice.** When a rewrite fails, the fallback
  short line follows the active persona (cat-flavored for the cat, neutral for everyone else).
  Detection ignores the modem sense of 猫 (「拨号上网的猫」 in the tech card is a dial-up modem,
  not the persona).
- `test_persona_apply_writes_config` asserted the old short preset text; persona apply now
  writes the card render and reports `has_character_card`.

### Changed

- `POST /api/personas/{id}/apply` prefers the character card render over the short preset
  blurb and records `source: character_card | preset` in the audit entry.

---

## [1.2.1] — 2026-11-03

Fixes found by running the whole stack against a real model, a real database and a real QQ
gateway. Every item below was reproduced first, then fixed, then locked down with a test.

### Fixed

- **Knowledge retrieval returned nothing for natural long questions.** `开发票需要多久？`
  scored zero hits while `发票` hit fine — the coverage gate demanded `max(2, n//2)` matching
  bigrams, and long questions spend most of their bigrams on question phrasing (`需要`/`多久`),
  so the single topical hit got diluted below the threshold. Net effect: the more naturally a
  user phrased a question, the less likely the knowledge base was found. Gate now scales with
  the number of content bigrams and keeps a floor of one match.
- **Question-phrasing words were treated as topic words.** `需要`, `多久`, `多长时间`, `几天`,
  `是否`, `能否` and similar are now in the filler list, so they no longer count toward
  retrieval overlap.
- **Citation badges never appeared.** The LLM reply path passed `parsed.citations` straight
  through, but the prompt never asks the model to emit citations, so the field was always
  empty — the console's `依据 KB005` badge could never render and users had no way to tell a
  grounded answer from a hallucinated one. Citations are now backfilled from the retrieval
  results when the model does not supply them.
- **Half-sentences were sent to users when the model hit its token limit.** Truncation was
  only handled when the content was *empty*; a reply that had content but was cut off mid
  answer was returned as a success. Reproduced live: `退货怎么弄` replied only
  `那我把步骤交出来喵` and dropped the entire return procedure. Content-bearing truncations
  now retry once with a larger budget and keep whichever answer is longer.
- **`t()` did not interpolate untranslated strings.** i18next only substitutes placeholders on
  a resource hit; this project uses Chinese source text as the key, so untranslated strings
  came back verbatim and the data page rendered a literal `schema v{v}`. Missing keys are now
  interpolated locally.
- **Stat card labels were clipped in English.** The cards were a single flex row, so
  `Total conversations` overflowed and rendered as `Total conve`. Now a two-row grid with
  wrapping labels; verified with a DOM overflow check in both languages.
- **Admin emotes API never returned vision tags.** Tags live in `tags.json` keyed by data
  path, but the endpoint read `it["tags"]` from the index, which never has that key — the
  console fell back to showing `image` for every item and tag search matched nothing, even
  after tagging the whole library.
- **`scripts/tag_emotes.py` hard-coded a model name** (`u2-med`) that 404s against the
  configured endpoint, and wrote its results only once at the end — a timeout lost the entire
  run. It now reads the configured model, falls back across providers, and flushes
  incrementally.
- **Gateway defaulted to the wrong core port.** `CORE_BASE_URL` defaulted to `8000` while core
  listens on `8010`. The gateway started cleanly and connected to NapCat, then posted every
  message into a dead port — from the user's side, "the bot ignores the group" with no visible
  error anywhere. Default aligned and a startup preflight now fails loudly.
- **`scripts/napcat_local.py` hung on unattended start.** It shelled out to NapCat's
  `launcher-user.bat`, which ends in `pause`; with no console to press a key, the process
  blocked forever and `run.log` contained only `请按任意键继续...`. It now invokes
  `NapCatWinBootMain.exe` directly and sets `NAPCAT_QQ_DATA_DIR` so the QQ kernel data
  directory is isolated from the desktop client.
- **`scripts/napcat_local.py` now reports QQ/NapCat version mismatch.** A QQ auto-update makes
  injection fail silently: the QQ process starts but stays at ~20 MB, no ports open, no logs
  written. `status` and `start` print an explicit warning instead.

### Added

- `frontend/src/lib/interpolate.ts` + tests — placeholder interpolation shared by `t()`.
- Regression tests for retrieval recall on long natural questions, citation backfill, emotes
  API tag exposure, and untranslated-string interpolation.
- `docs/QQ-LOCAL.md` troubleshooting for the two failure modes above, including the upstream
  NapCat issue reference.

### Changed

- `README.md` / `README.en.md` rewritten with current screenshots and an explicit
  limitations section.
- Landing page badges and links updated; test-count badge replaced with a version badge so it
  cannot go stale.

---

## [1.2.0] — 2026-11-02

Consolidates the engineering hardening work done after the 1.1.0 tag: persistence layer
decomposition, a new reminders module, Chinese numeral time parsing fidelity, and honest
capability documentation.

### Added

- `core/config_store.py`, `core/agent_store.py`, `core/tool_store.py` — persistence concerns
  extracted out of the 2,100-line `core/store.py`, each with its own focused test suite
  (`tests/test_config_store.py`, `tests/test_agent_store.py`, `tests/test_tool_store.py`).
- `core/reminder_parser.py` — self-contained reminder time parser (relative, absolute, clock,
  weekly, monthly expressions) with a fixed reference-time API; no service-layer imports.
- `core/api/reminders.py` — reminders REST router extracted from `core/main.py`.
- `core/version.py` + `VERSION` — single source of truth for the version string, surfaced by
  `/healthz` and `/api/agent/health`.
- `scripts/check_version.py` — guards against version drift between `VERSION`, `core/version.py`
  and `pyproject.toml`; wired into CI.
- `CHANGELOG.md`, `ROADMAP.md`, `docs/RELEASING.md` — release and roadmap management.
- Emote library console page (`/emotes`) with search, thumbnails and confirmed deletion.
- Dual-view run history page (`/runs`): agent tasks and action audit in one place.
- Windows-portable QQ end-to-end harness.

### Changed

- `core/main.py` route registration moved into `core/api/` packages where cohesion allows;
  SPA fallback must stay last (documented in `docs/ARCHITECTURE.md`).
- Dashboard charts now load on demand instead of shipping in the initial bundle.
- Settings console split from a 1,268-line file into a 283-line shell plus
  `src/settings/{bits,panels-core,panels-kb,panels-data}.tsx`.
- Console information architecture regrouped by task rather than by backend module.
- README rewritten to state verified capabilities and explicit limitations instead of
  promising everything works out of the box.

### Fixed

- Chinese numeral time expressions (`十分钟后`, `每天八点`) no longer lose their original
  span when routed to the reminder tool.
- `remember` rule routing returned empty arguments, causing a tool-level `TypeError`.
- Weekday reminders created on a weekend were silently dropped.
- Knowledge-base instruction material could leak verbatim into group chat replies.
- Three QQ tool routing gaps found on a real device: send, voice and proactive message.
- Windows subprocess output decoded as UTF-8 raised `UnicodeDecodeError` in the executor
  policy tests.

### Removed

- Feature branch `fix/qq-e2e-windows` — merged into `main`.

---

## [1.1.0] — 2026-10-29

First tagged release. Establishes the self-hosted QQ/Web secretary positioning and the
security baseline.

### Added

- Tiered action permissions (L0/L1/L2) with approval flow and hash-chained audit log.
- Knowledge base import (markdown, text, PDF, DOCX, XLSX), trial recall playground, export.
- Hybrid retrieval: SQLite FTS5 BM25 plus optional `sqlite-vec` vectors fused with RRF.
- Multi-provider LLM fallback chain with parallel racing, cooldown and content-level failover.
- Native function calling with JSON fallback for endpoints that lack tool support.
- MCP server exposing context-free tools to external MCP clients.
- Voice input (faster-whisper) and voice output (edge-tts), both optional.
- Persona system with 15 presets as a single backend source of truth.
- Prometheus metrics, request IDs, `/healthz` and `/readyz` probes.
- Docker multi-stage image with non-root runtime and health check.
- QQ gateway: NapCat / OneBot v11, group throttle, bot-to-bot loop guard, emote learning.
- Bilingual documentation site under `docs/`.

### Security

- Admin and channel token separation with constant-time comparison.
- `cryptography` pinned to 50.0 to clear known CVEs in 43.x/46.x.
- Path traversal and SSRF guards on executor and outbound HTTP clients.
- Secrets encrypted at rest with Fernet; masked on read-back.

---

## [1.0.0] — 2026-09-29

Initial public code drop: FastAPI core, React console, SQLite storage, rule-based reply engine,
LLM integration, and the first QQ channel adapter.
