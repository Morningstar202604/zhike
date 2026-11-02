# Roadmap

Direction and sequencing for Zhike. This is a living document: items move between
sections as they are scheduled, shipped or dropped. Shipped work is recorded in
[`CHANGELOG.md`](CHANGELOG.md).

Guiding constraint: Zhike is a **self-hosted personal secretary for QQ and the Web**.
Anything that does not make the assistant more reliable, more private, or easier to run
on your own hardware does not belong here — no plugin marketplaces, no visual flow
builders.

Legend: `P0` blocking · `P1` next · `P2` later · `P3` opportunistic

---

## Now — 1.3.0

| Pri | Item | Why | Acceptance |
|---|---|---|---|
| P0 | Finish persistence decomposition: `sessions`, `memory`, `actions` out of `core/store.py` | 2,100-line module is the main obstacle to review and to parallel work | Each new module owns its schema access; `store.*` compatibility shims stay; full suite green |
| P0 | Split remaining admin routes out of `core/main.py` into `core/api/{sessions,admin,actions,agent}.py` | 110 routes in one closure makes dependency wiring invisible | Route count unchanged, SPA fallback stays last, full suite green |
| P1 | Frontend i18n parity (currently ~36%) | The console advertises English but half of it is Chinese-only | Every user-visible string goes through `t()`; language switch changes the whole UI |
| P1 | Time-zone correctness for reminders across entry points | Tool entry and admin API currently disagree on the reference clock | One injected clock; regression test with a frozen non-UTC zone |

## Next — 1.4.0

| Pri | Item | Why | Acceptance |
|---|---|---|---|
| P1 | Split the 554 KB `echarts` chunk by module-level registration | Largest single asset; dashboard is behind auth so most users never need it | Dashboard renders with all chart types; no eager load |
| P1 | Promote `mypy` from advisory to blocking for new modules | Untyped growth is hard to reverse later | `mypy` clean for `core/api/*`, `core/*_store.py`, `core/reminder_parser.py` |
| P2 | Executor policy: signed command manifests instead of plain allowlists | Allowlist drift is the most likely way to grant more than intended | Manifest signature verified before execution; deny-by-default |
| P2 | Structured streaming events (typed SSE schema) | Frontend currently parses ad-hoc JSON per event name | Versioned event envelope; frontend types generated from it |

## Later — 2.0.0

| Pri | Item | Why | Acceptance |
|---|---|---|---|
| P2 | Multi-tenant isolation with per-tenant keys and quotas | Needed for small-team hosting, which users already attempt | Tenant scoping enforced in storage layer, not route layer |
| P3 | Plugin interface for custom tools loaded from a directory | Requested repeatedly, but must not become a marketplace | Static, signed, opt-in loading; no remote install |
| P3 | Optional Postgres backend behind the same storage interface | SQLite is right for one box; some deployments outgrow it | Storage interface fully abstracted; both backends pass the same suite |

## Explicitly not planned

- Visual agent/flow builder — conflicts with a rule-first, auditable assistant.
- A plugin marketplace or remote plugin install — supply-chain risk with no clear win.
- Hosted SaaS — the point is that you run it yourself.
- Voice cloning / synthetic persona media — abuse surface outweighs the value here.

---

## How items get here

1. Something breaks in real use, or a limitation blocks a real deployment → candidate.
2. It gets a one-line "why" and a measurable acceptance criterion above.
3. Only then does it get scheduled into a version.

An item without an acceptance criterion is not a roadmap item; it is a wish.
