# Release Process

Operational runbook for cutting a Zhike release. Written so a maintainer who has not
touched the project in three months can follow it end to end without guessing.

---

## 1. Versioning

Zhike follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

| Bump | When | Examples |
|---|---|---|
| MAJOR | Incompatible change for operators or API consumers | Removing a config key, renaming an endpoint, a schema migration that requires manual steps |
| MINOR | Backward-compatible capability | New tool, new console page, new optional dependency |
| PATCH | Backward-compatible fix | Bug fix, performance work, dependency patch, docs |

Pre-release suffixes (`1.3.0-rc.1`) may be used on a release branch; they are never tagged
on `main`.

The version string lives in exactly three places and all three must agree:

1. `core/version.py` — `VERSION`
2. `VERSION` — repository root
3. `pyproject.toml` — `[project] version`

`python scripts/check_version.py` verifies this and is part of CI.

---

## 2. Branch policy

`main` is the only long-lived branch. Everything else is short-lived and deleted after merge.

```
main ──┬── feat/short-description ──► squash-merge ──► main
       └── fix/short-description  ──► squash-merge ──► main
```

Rules:

- Branch from `main`, merge back into `main`. No `develop`, no release branches unless a
  release needs to be stabilized while `main` moves on.
- Branch names: `feat/`, `fix/`, `refactor/`, `docs/`, `chore/` + kebab-case description.
- Squash-merge keeps `main` linear and makes `git bisect` usable.
- After merge, delete the branch locally **and** on every remote.

---

## 3. Release checklist

Run from a clean checkout of `main`.

### 3.1 Pre-flight

```bash
git switch main
git pull --ff-only
python scripts/check_version.py
```

- [ ] Working tree clean, no stashes.
- [ ] `CHANGELOG.md` has an entry under `## [Unreleased]` describing everything since the
      last tag. Rewrite it for readers, not for the commit log.
- [ ] `ROADMAP.md` reflects what actually shipped (move items out of *Now*).
- [ ] Version bumped per the table in section 1.
- [ ] `VERSION`, `core/version.py`, `pyproject.toml` in sync.

### 3.2 Backend gates

```bash
.venv/Scripts/python.exe -m ruff check core executor gateway scripts tests
.venv/Scripts/python.exe -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning
```

- [ ] Ruff: no findings.
- [ ] Pytest: all pass, skips explained. Any new skip needs a reason string.
- [ ] Coverage did not drop below the CI threshold (70%).

### 3.3 Frontend gates

```bash
cd frontend
npx tsc --noEmit
npx oxlint src
npm test
npm run build
```

- [ ] Type check clean.
- [ ] Lint: zero errors (warnings are tracked, not blocking).
- [ ] Unit tests pass.
- [ ] Production build succeeds; `dist/` regenerated.

### 3.4 Runtime smoke

```bash
CS_AGENT_STATIC_DIR=frontend/dist .venv/Scripts/python.exe -m uvicorn core.main:app --port 8010
```

- [ ] `GET /healthz` returns `{"ok": true, "version": "<expected>"}`.
- [ ] `GET /readyz` returns 200 with `db`, `kb`, `llm`, `search` sections.
- [ ] `/` serves the console.
- [ ] Log in with the admin token, open Dashboard, Sessions, Knowledge, Reminders, Runs.
- [ ] No console errors in the browser devtools on any visited page.

Screenshots for the README are regenerated in this step (see `scripts/take_screenshots.py`).

### 3.5 Community files

- [ ] `SECURITY.md` still lists a working contact.
- [ ] `CONTRIBUTING.md` commands still run.
- [ ] Issue/PR templates still match how the project actually works.

### 3.6 Tag and publish

```bash
git add -A
git commit -m "chore(release): vX.Y.Z"
git tag -a vX.Y.Z -m "Zhike vX.Y.Z"
git push origin main
git push origin vX.Y.Z
```

- [ ] Tag is annotated, not lightweight.
- [ ] Release notes on the hosting platform are the `CHANGELOG.md` section, not "see commits".
- [ ] Mirror remotes are updated in the same pass (see section 4).

### 3.7 Post-release

- [ ] Move `## [Unreleased]` content under the released heading and open a new empty
      `## [Unreleased]`.
- [ ] Verify `GET /healthz` on a deployed instance reports the new version.
- [ ] If the release changed configuration keys, update `.env.example` and `DEPLOY.md`.

---

## 4. Mirrors

Zhike is published on several platforms. They are mirrors of the same history, not
independent forks.

| Platform | Remote name | Notes |
|---|---|---|
| GitCode | `origin` | Primary; CI hooks run server-side |
| Gitee | `gitee` | Mirror |
| GitHub | `github` | Added on request; GitHub Actions is the only CI that runs workflows |

Mirror rules:

- Push `main` and the release tag to every configured mirror.
- Never force-push a mirror to "fix" divergence — investigate first.
- Keep repository descriptions identical across mirrors (English primary, Chinese secondary).

---

## 5. Rollback

If a release is bad:

1. Revert the release commit on `main` (`git revert`), do not rewrite history.
2. Cut a PATCH release with the fix.
3. If the database migration is at fault, restore from the documented backup path
   (`docs/ARCHITECTURE.md`, storage section) before rolling back the code.
