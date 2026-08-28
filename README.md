# mediadeck

Self-hosted unified control panel for an Emby-based media stack.

One panel to operate what is usually scattered across many tools:

- **Streaming node management** — multi-node 302 redirect scheduler with
  load-aware dispatch (active streams / egress bandwidth / weights), health
  probing, manual drain & kick.
- **Pipeline overview** — staging/upload queues, quota state, oldest stuck
  items, emergency-local fallback lane, all read-only from your existing
  worker state files.
- **User management** — Emby users, policies, device limits, invite codes.
- **Import lanes** — cloud-drive (115 / Google Drive link) importers, PT
  search/subscribe via MoviePilot as the engine behind the panel.
- **Identify & scrape review** — AI identification audit queue, manual TMDB
  correction.

## Design rules

- **Zero data in repo.** All endpoints, tokens, paths come from environment /
  local config (`.env`, gitignored). The repo ships `.env.example` only.
- Backend: FastAPI (Python 3.11+). Frontend: Vue 3 + Vite (added later —
  functionality first, UI polish last).
- Every external system sits behind an adapter with a mock implementation, so
  the full panel runs locally with `MEDIADECK_MOCK=1` and zero real
  credentials.

## Dev quickstart

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # fill in, or set MEDIADECK_MOCK=1
uvicorn app.main:app --reload --port 8300
# open http://127.0.0.1:8300/docs
```

## License

MIT
