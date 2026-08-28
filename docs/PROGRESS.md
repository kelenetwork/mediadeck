# Progress Ledger

Newest entries first. Every working session appends one entry.

---

## 2026-08-28 (2) — pipeline overview + node probe agent
**Done**
- `app/modules/pipeline.py`: PipelineReader serves a sanitized JSON snapshot
  written by a host-local collector (real paths never enter the repo);
  staleness detection (>300s); MockPipeline for credential-free dev.
- `agent/loadprobe.py`: stdlib-only single-file load probe for streaming
  nodes — /load endpoint reporting ESTABLISHED-connection stream count and
  5s-window egress Mbps, optional bearer token.
- Wired `/api/pipeline` into the app; +1 test (6 passing), ruff clean.

**Next**
- Dispatch log + probe history in scheduler.
- Host-side collector script (lives outside this repo, on the operator host).

**Open questions**
- none new.

---

## 2026-08-28 — Phase 1 scaffold
**Done**
- Repo initialized (public), FastAPI backend skeleton.
- Config strictly from env (`.env.example` only in repo); mock/live adapter
  architecture so the whole panel runs credential-free with `MEDIADECK_MOCK=1`.
- Load-aware 302 stream scheduler: normalized load = active_streams/weight,
  health probing with failure threshold, manual disable/enable, dispatch
  dry-run endpoint, real `/stream/{path}` 302 edge.
- Emby adapter (users, active sessions) in mock + live variants.
- 5 smoke tests passing; dev workflow codified in DEVELOPMENT.md.

**Next**
- Pipeline overview module: read-only queue/quota state via a host-local
  collector script that exports sanitized JSON (keeps real paths out of repo).
- Node probe agent (single-file, deployable to streaming nodes).

**Open questions**
- Panel domain: undecided (owner: "whatever for now").
- Frontend stack decision deferred until Phase 4 UI pass.
