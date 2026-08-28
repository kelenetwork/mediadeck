# Development Workflow (mandatory)

This file is the contract for how mediadeck is developed. All work — human or
AI-assisted — follows it. No exceptions.

## 1. Repo-first rule

- **All code changes happen in this repository via git.** Never edit a deployed
  copy directly; deployment hosts only ever run a checked-out tag/commit.
- `main` is always deployable. Broken `main` is a stop-the-line incident.

## 2. Branch / merge flow

```
feature/<topic>  ->  PR  ->  CI green + self-review  ->  squash-merge to main
fix/<topic>          (same)
```

- One topic per branch. Small, reviewable diffs.
- Commit messages: imperative summary line, body explains WHY when non-obvious.
- Merges to `main` come only from PRs; no direct pushes after Phase 1 scaffold.

## 3. Local test before merge

Every PR must pass locally before it is opened:

```bash
cd backend
. .venv/bin/activate
ruff check app tests          # lint
python -m pytest tests/ -q    # all tests green
MEDIADECK_MOCK=1 uvicorn app.main:app --port 8300   # boots in mock mode
```

- New features ship with tests in the same PR.
- Mock adapters must cover every new external integration so the panel always
  runs credential-free (`MEDIADECK_MOCK=1`).

## 4. Data isolation (hard red line)

- This is a public repository. **No real endpoints, IPs, domains, tokens,
  usernames, file paths of the production stack, or media titles** may appear
  in code, tests, fixtures, docs, or commit messages.
- Configuration enters only via environment (`.env`, gitignored). The repo
  ships `.env.example` with placeholder values only.
- Before every push: `git diff` review specifically for leaked values.

## 5. Progress tracking

- `docs/PROGRESS.md` is the single progress ledger. Every working session ends
  with an entry: date, what changed, what's next, open questions.
- Phase plans live in `docs/ROADMAP.md`; scope changes are edited there first,
  then implemented.

## 6. Releases / deployment

- Deployable states are tagged `v0.x.y`.
- Deployment = `git fetch && git checkout <tag>` on the host + service restart.
  Rollback = checkout previous tag.
- The deployment host's `.env` is never touched by git.
