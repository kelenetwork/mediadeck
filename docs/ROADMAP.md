# Roadmap

## Phase 1 — scaffold + stream scheduling (current)
- [x] FastAPI skeleton, settings-from-env, mock/live adapter split
- [x] Load-aware 302 scheduler (weights, health, manual kick)
- [x] Emby adapter: users + active sessions
- [x] Smoke tests, mock mode boots credential-free
- [x] Pipeline overview module (queue depths, quota states, oldest stuck items)
- [x] Node probe agent (tiny /load endpoint to run on each streaming node)
- [x] Scheduler: probe history + dispatch log

## Phase 2 — import lanes + user management
- [ ] Cloud-drive import module (port of existing internal dashboard)
- [ ] Drive-link import module
- [ ] Emby user management: create/disable/policy templates, invite codes

## Phase 3 — acquisition shell + review
- [ ] MoviePilot adapter (search/subscribe/download/history) behind thin API shim
- [ ] Identify/scrape review queue (AI verdicts, manual metadata correction)
- [ ] Notification center with routing rules

## Phase 4 — replace external components
- [ ] Request management (replaces external request tool)
- [ ] Registration/invite system (replaces external bot)
- [ ] Frontend polish pass (UI style decided by owner at this stage)
