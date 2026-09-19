# 0009 — A suspend cure kills the child's pid tree and warm-resumes in place

**Context.** Worker children are spawned without `start_new_session`
(`sessions.py`, `_spawn`), so they share the driver's process group — the one
`setsid` created when the run-driver skill launched the run. The driver's own
SIGINT handler kills no children; today they die only because the operator's
`kill -INT -<pgid>` hits the whole group, driver included. The brainstorm for
stall detection (2026-09-16) wrote the automatic cure as "SIGTERM the child's
process group", which in this layout would kill the driver too.

**Decision.** The Suspend Cure signals the child pid and every descendant found
by walking `/proc` `ppid` chains (SIGTERM, grace, SIGKILL), never a process
group. The blocking worker call then fails with a typed `SuspendCured`, and the
group's review loop re-issues a warm resume of the same session id in the same
generation — no scheduler Re-entry, no `reentry_count` increment, its own capped
`cures` counter.

**Why.** Giving children their own session would scope a `killpg` cleanly but
would also stop a foreground Ctrl-C from reaching workers, forcing new
terminate-on-exit code on the SIGINT path; the pid-tree walk is code the Sign of
Life evaluation already needs. Reusing the scheduler's Re-entry would quarantine
a nightly-suspending laptop's groups within days (the r20260908 g8 failure
0.17.2 fixed for operator signals).
