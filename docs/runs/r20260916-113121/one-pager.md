# Stall detection and decision carry — r20260916-113121

## TL;DR

Twelve groups ran serially from launch commit 8b9da2b; every group passed on its first attempt except g4, which retired once and asked one question.

- Stall detection now works on a real worker: a SIGSTOPped `claude` reads `not live for 29s … cpu flat`, then `live again`, proven by the live tier outside the sandbox (tests/test_liveness_live.py)
- Binding operator decisions are recorded as a ledger derived from the escalation files and carried into every later coder and reviewer prompt (g12)
- The liveness plumbing shipped unwired from g6 and g8 and was wired by the driver in commit 30ff9f1 before g4 merged (orchestrator/execution/review.py)

## Problems found

Two defects slipped through self-verified groups whose every check was a unit test against fakes.

- Nothing constructed `ActivityRegistry` or `LivenessProbe` in a real run, so no heartbeat carried Sign of Life facts and the Suspend Cure never fired (g8)
  g8's coder flagged it as a surprise; g6 and g8 still passed 10/10 and 6/6.
- Signal (b) counted any child process as life, and a real `claude` keeps its MCP servers as children all session, so Not Live never fired on a real worker (orchestrator/execution/liveness.py)
- A coder cannot run the live tier: a nested `claude` cannot write its transcript from inside the coder sandbox, same for tests/test_e2e_live.py (g4/coder/gen2)
- The live test thawed the worker as soon as `status` showed Not Live, before the probe's 15 s tick had logged it, so `live again` never appeared (tests/test_liveness_live.py)
- g4 gen1 retired at 253,891 context tokens against the 250,000 breaker after two rounds and $8.19 (g4/coder/gen1)
- g11-4 and g6-10 real-run checks ran in worktrees with no `.orchestrator/runs/`, so they exercised no real run data (g11)
- A UI test, `routes.test.tsx` job routes (U23) streaming, failed the merge gate once and passed 5/5 on rerun (c50265ef1049)

## Run notes

The driver launched detached with HITL on at `on_stuck` and a 4 h escalation timeout, serial per config.

- c50265ef1049, preflight_failed on g3: the diff touched only retry.py, the failing UI test passed 5/5 in the worktree and the UI suite passed 199/199; answered `retry` with no text and g3 merged (c50265ef1049)
- The driver's watch lapsed from 13:33 and bc558f05e22d, a coder_question from g4 gen2, waited about 11 hours unanswered (bc558f05e22d)
- The human chose to wire the plumbing when g4 escalated and to require a real live-tier pass; the driver added the wiring plus tests/test_liveness_wiring.py (tests/test_liveness_wiring.py)
- First live-tier run failed on MCP-server children; the human decided a tool child starts after the worker's first assistant event, implemented via `first_assistant_at` and `/proc` start time (orchestrator/execution/liveness.py)
- Second run failed on the thaw race; the test now waits for `not live for` before SIGCONT; third run passed in 54.93 s, full suite 1929 passed (tests/test_liveness_live.py)
- Driver commit 30ff9f1 went into g4's worktree; bc558f05e22d was answered with that evidence and g4 merged 7/7 (g4/coder/gen2)
- The CLI committed the driver's untracked one-pager scaffold as stranded integration work at g4's merge (docs/runs/r20260916-113121/one-pager.md)

## Next steps

- Make the live tier part of acceptance: a plan item that needs a real `claude` has to be run by the driver, and the plan should say so, or it ends up skipped (tests/test_liveness_live.py)
  - how: add a "driver-run" tag to Run: items in /orchestrator-plan and a triage-guide step for it
- Add a wiring check to plans that build a mechanism: every new registry or probe needs a test that asserts the real `_cmd_run` construction site, as tests/test_liveness_wiring.py does (g8)
- Stabilise the U23 streaming job-log test: it fails under merge-gate load, and each flake costs a gate rerun and a driver triage (c50265ef1049)
- Re-run the g11-4 and g6-10 real-run checks from the main checkout against its real `.orchestrator/runs/` (g11)
- Stop the CLI committing a driver's one-pager scaffold as stranded work, or document that writing it early is expected (docs/runs/r20260916-113121/one-pager.md)
- Size g4-class groups below the breaker: cli-surfaces plus evidence retired at 253,891 tokens (g4/coder/gen1)
