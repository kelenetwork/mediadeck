# Progress Ledger

Newest entries first. Every working session appends one entry.

---

## 2026-08-29 (7) — storage management backend (v0.8.0)
**Done**
- Panel becomes a configuration entry point, not just a viewer: cloud remotes
  and mounts can now be created from the API instead of being hand-edited on
  the host.
- `app/modules/storage.py`: StorageManager (configparser-based remote CRUD with
  atomic writes, connectivity test, systemd unit generation, start/stop/delete)
  plus MockStorage for credential-free development.
- Settings: rclone_binary, rclone_config_path, mount_root, cache_root,
  systemd_unit_dir, systemd_unit_prefix.
- Nine /api/storage/* routes with ValueError -> 422 and other failures -> 409.
- Security gates reviewed line-by-line and verified by hand: name allowlist
  regex, realpath containment against mount root (blocks ../, absolute paths
  and nested traversal), list-form subprocess with no shell, fixed unit-name
  prefix, secret redaction on read, explicit not-configured errors.
- Implementation delegated to a coding subagent; reviewed in depth (this
  touches production writes and permission logic) before commit.
- Tests 16/16, ruff clean.

**Next**
- Storage management UI page.
- Full-replacement roadmap (own scraper/library/playback) per owner decision.

---

## 2026-08-29 (6) — scheduled task center (v0.7.0)
**Done**
- `app/modules/tasks.py`: TasksReader over a sanitized host snapshot (missing
  file / unreadable JSON fail-safe, stale after 600s) + MockTasks covering ok,
  failing-with-streak, never-run and disabled jobs.
- `tasks_snapshot_path` setting, `GET /api/tasks`, env placeholder.
- Panel page 调度中心: three stat cards (total / currently failing / disabled),
  task table (schedule, status tag, relative last-run, duration, failure
  streak highlighted) and an alert card.
- First task produced under the new split-role workflow: implementation was
  delegated to a coding subagent, then reviewed line-by-line and committed by
  the main agent. Review notes: pattern-consistent with mounts.py, all HTML
  interpolation escaped, no real identifiers, null last_run handled.
- Tests 15/15, ruff clean.

**Next**
- Host-side task collector (outside repo) exporting the sanitized snapshot.
- v0.8.0 invites & access.

---

## 2026-08-29 (5) — mount health module (v0.6.0)
**Done**
- Rewrote ROADMAP into a self-driving plan (v0.6 mounts -> v0.7 scheduled
  tasks -> v0.8 invites/access -> v0.9 reports/notifications -> v1.0 live
  import executor + UI pass); no longer waits for per-step direction.
- `app/modules/mounts.py`: MountsReader over a sanitized host snapshot,
  MockMounts for dev. GET /api/mounts.
- Panel page 挂载管理: alive/stuck/cache stat cards, per-mount table
  (kind, options, readdir latency, stuck-process count, cache usage vs
  limit, free space) and an alert list.
- Host collector (outside repo) probes readdir in a child process so a wedged
  FUSE mount cannot hang the collector, counts D-state processes per mount,
  measures VFS cache dirs, and flags a union mount missing allow_other —
  the exact failure that took the library down earlier today.
- Tests 14/14, ruff clean.

**Next**
- v0.7.0 scheduled task center.

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

## 2026-08-29 (3) — node provisioning, signed URLs, settings-page fix
**Owner-reported issues, all root-caused**
1. *Panel feels laggy* — `/api/emby/libraries` took 2.16s: one item-count query
   per library (N+1), re-run on every page render and every 30s auto-refresh.
   Added a shared TTL cache (libraries 120s, sessions 5s).
2. *Settings page fails to load* — **my bug, shipped in v0.9.0**: `/api/settings`
   never returned `playback`, but the page did `const p = s.playback` and then
   read `p.enabled`, throwing before anything rendered. Added the field and
   pinned the whole payload shape with `test_settings_overview_contract`, since
   this class of bug is invisible until the page is opened.
3. *Nodes are unconfigurable / "云里雾里"* — correct, and the most important
   one. The panel could register a node but never explained how to build one;
   registering a node that does not exist only produces 404s.
4. *Security* — unsigned node URLs are permanent public download links.

**Done**
- `app/modules/signing.py`: nginx `secure_link_md5` compatible signed URLs with
  expiry. Digest is computed over the **decoded** `$uri` and only the result is
  percent-encoded — the reverse order 403s every path with a space or CJK
  character, i.e. most of a Chinese library. Key rotation invalidates every
  link already handed out.
- `app/modules/provisioning.py`: renders the whole node stack from stored
  settings — rclone mount unit (VFS cache, read-only, `Before=nginx`), nginx
  vhost (secure_link, range support, autoindex off), loadprobe unit, and a
  single install script wiring them together with self-checks. Also renders the
  Caddy/nginx front-door rule that routes **only** stream paths to the panel,
  which is the missing answer to "how does emby.example.com dispatch to nodes".
  Nothing is executed and no remote host is contacted; it emits reviewable text.
- Panel serves `/agent/loadprobe.py` so the installer fetches the agent from
  the panel itself and there is exactly one copy of it in the repo.
- Settings page gained 链接安全（签名）and 接入方式 cards; node page can fetch
  its install script.
- Settings/update pages no longer auto-refresh: re-rendering mid-edit wiped
  whatever the operator was typing.

**Verified**
- Signed URL round-trip incl. CJK path; expiry and wrong-path rejection.
- Install script contains every layer (mount, VFS cache, secure_link with the
  shared secret, `user_allow_other`, certbot, probe) and no unrendered template.
- Front-door snippet diverts only `emby/Videos/.../stream`.
- 38 tests passing, ruff clean.

**Next**
- Node-side verification against a real server (ca1) once owner approves.
- Invite codes / access control.

**Open questions**
- Install script assumes Debian/Ubuntu + systemd + nginx. Other distros need
  either detection or a documented manual path.

---

## 2026-08-29 (2) — playback interception (multi-node becomes real)
**Done**
- `app/modules/playback.py`: PlaybackRouter — Emby-compatible stream edge at
  `GET /emby/Videos/{id}/{rest}`. Resolves the item's backing file via Emby,
  uses that **file path** (not the request URL) as the affinity key, and 302s
  the client to the chosen node's copy. Until now the scheduler only had the
  `/stream/` test edge, so no real client ever reached it.
- **Fail-open by construction**: disabled, transcode/HLS, missing `Static=true`,
  unresolved item, no healthy node, empty mapped path and Emby errors all fall
  through to the Emby origin. A panel bug can only mean "not accelerated this
  time", never "playback is broken".
- Operator-configurable path mapping (`strip_prefix` + `path_template`) because
  a node's media root rarely matches Emby's; `{path}` placeholder enforced.
  `GET /api/playback/preview?item_id=` dry-runs the mapping so misconfiguration
  is caught in the UI instead of as 404s in client logs.
- TTL cache (300s) on item->path lookups so a popular title starting on many
  clients does not become one Emby metadata call per client; negatives cached
  too. Cache invalidated on settings save.
- `LiveEmby.item_media_paths()` maps MediaSourceId -> on-disk path, skipping
  http(s) sources that have no local file to serve.
- Decision log (`/api/playback/log`) records reason/node/media_path per
  interception, so "why did this not accelerate" is answerable.
- Interception defaults to **off** and refuses to enable without a node.
- Panel: 播放分流 card in 系统设置 (toggle, direct-only, path mapping, preview).

**Fixed while testing**
- Mock demo nodes were fabricated in `main.py` and never entered the settings
  store, so the settings page showed 0 nodes while the node page showed 2 —
  the same panel contradicting itself, and the "needs a node" guard misfiring.
  Demo nodes now seed through the store like real ones; `_mock_nodes()` removed
  and the scheduler reads nodes from a single source of truth.

**Verified** (live mock instance)
- Real Emby path `/emby/Videos/item42/stream.mkv?Static=true` -> 302 to node.
- Affinity: same item 10/10 to one node; 30 distinct items split 17/13.
- Fail-open: m3u8, no-Static, unknown item, disabled -> all to Emby origin.
- Path template `files/{path}` -> `https://node/files/Movies/Demo/item7.mkv`.
- 28 tests passing, ruff clean.

**Next**
- Node agent: serve mapped media paths (currently the node side is assumed).
- Invite codes / access control (v0.8.0).

**Open questions**
- Node-side auth: clients get a plain node URL. Signed/expiring URLs are needed
  before this is exposed to untrusted users commercially.

---

## 2026-08-29 — settings center + affinity dispatch
**Done**
- `app/core/store.py`: JSON-backed runtime settings document (atomic write,
  mode 600, gitignored data dir). Operator config no longer lives in `.env`.
- `app/modules/settings.py`: settings service — Emby connection, dispatch
  policy and streaming nodes are all editable via API/UI and applied to the
  running process immediately (scheduler is reconfigured in place, no restart).
  API keys are returned masked only; a `__KEEP__` sentinel lets the operator
  edit a URL without re-typing the secret.
- `LiveEmby` now resolves its connection per call from the settings store
  instead of frozen env vars; added `system_info()` + standalone `probe_emby()`
  so the UI can test a connection before saving it.
- Typed domain errors (`ConfigError` / `NotConfigured` / `UpstreamError`) with
  FastAPI handlers -> 422 / 409+needs_setup / 502, so the UI can distinguish
  "you typed something wrong" from "not connected yet" from "Emby is down".
- Scheduler: added `affinity` policy (rendezvous hashing) alongside
  `least-load`. Same path always resolves to the same node, so a title is
  cached once instead of pulled from origin by every node; falls through to the
  next candidate when the preferred node is unhealthy or above the utilisation
  threshold. Dispatch log records policy + reason.
- **Fixed a real modelling bug found during verification**: the old `weight`
  field made "normalized load" an absolute stream count, so comparing it to a
  0-1 threshold always overflowed and every request fell back to least-load
  (60 distinct files all landed on one node). Replaced `weight` with absolute
  `capacity`; load is now `active_streams / capacity`, so one threshold is
  meaningful across a heterogeneous fleet.
- Panel: new 系统设置 page (Emby connect + test, dispatch policy, node summary);
  node page gained add/edit-capacity/delete and shows utilisation %.
- Tests isolated per-test via `conftest.py` (own data dir), +6 tests (22 total),
  ruff clean.

**Verified**
- Live mock instance: same path -> same node 20/20; 60 distinct paths spread
  19/22/19 across three nodes. Secret persisted with mode 600, never returned
  in cleartext; URL-only edit retains the stored key.

**Next**
- Node agent config distribution (push rclone/mount config from the panel).
- Emby PlaybackInfo middleware so real playback uses the 302 scheduler.

**Open questions**
- Panel auth is still a single HTTP-Basic admin; multi-operator accounts and
  audit logging are needed before this is commercially usable.

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
