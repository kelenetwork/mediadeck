# Roadmap

Owner sets priorities when he wants to; otherwise this plan is executed
top-down without asking. Every item ships as its own PR + release tag.

## Done
- [x] Backend scaffold, env-only config, mock/live adapter split
- [x] Load-aware 302 stream scheduler (weights, health, kick, history, log)
- [x] Node load probe agent (stdlib single file)
- [x] Pipeline overview (sanitized host collector -> panel)
- [x] Emby user management (create/disable/password/policy)
- [x] Import lane skeleton (job lifecycle + API)
- [x] Web-triggered self-update to release tags
- [x] Admin panel shell (grouped sidebar, stat cards, page registry)
- [x] Media library overview
- [~] Acquisition/downloads: dropped by owner decision (stays external)

## v0.6.0 — Mount health (next)
Storage is the single most failure-prone layer in this stack: FUSE mounts go
stale, a union mount loses allow_other and the whole library 403s, ffprobe
wedges in D-state. This page makes all of it visible in one place.
- [ ] Collector: per-mount liveness (readdir probe), backend type, options
- [ ] Detect stuck I/O (processes blocked in uninterruptible sleep per mount)
- [ ] VFS cache usage vs configured limits; free-space floor per filesystem
- [ ] Panel page: mount table, health tags, stuck-process alerts

## v0.7.0 — Scheduled task center
Dozens of guards/workers run on cron; today their state is only visible by
reading logs on the host.
- [ ] Collector: per-job last run, exit status, duration, next due
- [ ] Failure streak detection and surfacing on the dashboard
- [ ] Panel page: job table, last output tail, manual trigger (allowlisted)

## v0.8.0 — Invites & access
- [ ] Invite code issuing with quota/expiry, redemption -> Emby user creation
- [ ] Access control view: per-user device/stream limits at a glance

## v0.9.0 — Playback reports & notifications
- [ ] Playback history aggregation (top titles, per-user minutes, node split)
- [ ] Notification center with routing rules and delivery log

## v1.0.0 — Live import executor + UI pass
- [ ] Bridge import jobs to real host-side workers (sanitized IPC)
- [ ] Visual design pass across all pages
