# Progress Ledger

Newest entries first. Every working session appends one entry.

---

## 2026-08-29 (4) — drop acquisition module, add media library page (v0.5.0)
**Done**
- Owner decision: download/acquisition management stays out of scope. Removed
  the MoviePilot adapter, all /api/mp/* routes, its settings fields, env
  placeholders, tests and the two panel pages that used it.
- New media library module: EmbyAdapter.libraries() (mock + live via
  VirtualFolders + per-library item counts), GET /api/emby/libraries, and a
  媒体库 page with stat cards + library table. Dashboard now shows library
  counts instead of subscription/download counts.
- Tests 13/13, ruff clean.

**Next**
- Fill remaining pages step by step (invites, scheduled tasks, playback
  reports, mount management) per owner priority.

---

## 2026-08-29 (3) — panel shell redesign (v0.4.0)
**Done**
- Replaced the flat tab page with a proper admin shell: grouped left sidebar
  (概览 / 工作台 / 资源服务 / 系统管理), sticky topbar with page title+subtitle,
  hash routing, 30s auto refresh, toast layer.
- Split static assets into index.html + app.css + app.js (mounted at /static);
  new /api/whoami for the sidebar identity block.
- Pages: dashboard (6 stat cards + sessions/queues/quota/alerts), 搜索订阅,
  下载任务, 网盘上片, 用户管理, 节点管理, 管线状态, 版本更新.
- Page registry (PAGES) so new modules only add one entry + one nav item.
- Tests 13/13, ruff clean.

**Next**
- Fill pages step by step per owner feedback (media library, invites,
  scheduled tasks, playback reports).

---

## 2026-08-29 (2) — MoviePilot acquisition integration (v0.3.0)
**Done**
- `app/adapters/mp.py`: LiveMoviePilot (bearer login w/ token cache + renew on
  401) and MockMoviePilot. Media recognition search, site torrent search,
  subscribe add/list/delete, push download, downloading list.
- Endpoints under /api/mp/*; panel gets a new 搜索/订阅 tab (media search ->
  one-click subscribe w/ season prompt; torrent search -> one-click download;
  subscription table w/ unsubscribe; active downloads w/ progress).
- +1 test (13 passing), ruff clean.

**Next**
- Live import executor bridging host cloud-drive workers.
- Invite codes; notification center.

---

## 2026-08-29 (1) — basic functional web UI (v0.2.0)
**Done**
- Single-file functional panel at / (auth-protected): overview (sessions,
  pipeline queues, quota, fallback, alerts), stream nodes (status + dispatch
  log + enable/disable), Emby users (create/disable/enable/password), import
  jobs (submit/progress/cancel), update tab (check + one-click apply).
- Plain functional styling only; the visual design pass stays in Phase 4.
- Tests 12/12, ruff clean.

---

## 2026-08-28 (7) — root path redirect (v0.1.1)
**Done**
- GET / now 307-redirects to /docs so the panel root is not a bare 404.
- +1 test (12 passing), ruff clean. Released as v0.1.1 to exercise the
  web-triggered update flow end to end.

---

## 2026-08-28 (6) — self-update from the web panel
**Done**
- `app/modules/updater.py`: version (git describe), check origin for newest
  semver release tag, apply update via detached helper (fetch tags, checkout,
  pip install, service restart) so the API process can die safely mid-update.
- Endpoints: GET /api/update/version, GET /api/update/check,
  POST /api/update/apply (409 when already up to date / no valid tag).
- +2 tests (11 passing), ruff clean.

**Next**
- First release tag v0.1.0 + local deployment as a systemd service.
- Live import executor; invite codes.

---

## 2026-08-28 (5) — import lane module skeleton
**Done**
- `app/modules/imports.py`: unified ImportJob lifecycle (queued/running/done/
  failed), ImportManager registry with executor delegation, MockExecutor
  simulating progress. Kinds: cloud-drive, drive-link.
- Endpoints: POST /api/imports, GET /api/imports (+state filter),
  GET /api/imports/{id}, POST /api/imports/{id}/cancel.
- +1 test (9 passing), ruff clean.

**Next**
- Live executor adapter bridging host-side import workers (sanitized IPC).
- Invite-code system.

---

## 2026-08-28 (4) — Emby user management (write ops)
**Done**
- EmbyAdapter contract extended: create_user, set_user_disabled,
  set_user_password, apply_policy (mock + live implementations).
- Endpoints: POST /api/emby/users, /{id}/disable|enable, /{id}/password,
  /{id}/policy (policy patch restricted to an allowlist of safe fields).
- +1 test (8 passing), ruff clean.

**Next**
- Import-lane module skeleton (cloud-drive importers, Phase 2).
- Invite-code system design.

---

## 2026-08-28 (3) — scheduler dispatch log + probe history
**Done**
- Scheduler keeps per-node probe history (deque, ~3h at 15s interval) and a
  recent dispatch decision log (node chosen, normalized load, candidate count,
  request context). Dry-run picks are NOT recorded; real /stream 302s are.
- New endpoints: `GET /api/nodes/{name}/history`, `GET /api/dispatch/log`.
- +1 test (7 passing), ruff clean.

**Next**
- Host-side collector script (outside repo) exporting sanitized pipeline snapshot.
- Roadmap Phase 2 prep: import-lane module skeleton.

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
