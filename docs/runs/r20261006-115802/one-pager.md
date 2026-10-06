# Worker friction, live breakers and the export the analyses need — r20261006-115802

## TL;DR

Nine units from the 2026-10-06 plan, run serially on sonnet from `89faf361` with HITL at `on_stuck`.

- 8/8 groups completed and 9/9 units landed, every group on its first round and with zero spec rewrites (g8)
- $6.13 across 9 sessions; the only repeated work was one merge-gate re-run after a cross-group test seam (898941bcac83)
- Workers now get `--add-dir` for their own run directory, `TMPDIR` inside the worktree, 24-character slugs, a per-round `RoundSignals` reading with the context ladder on by default, a verification-omission gate, export bundle v3 and a persisted rewrite counter (89faf361)

## Problems found

Nothing blocked the run; every item below surfaced at a gate or in the driver's own verification.

- g7's merge gate failed six tests in `tests/test_turn_observer.py`: g5 stubbed the session entry as `SimpleNamespace(last_context_tokens=0)`, and U7 added `entry.peak_context_tokens` to the same per-turn bookkeeping (898941bcac83)
- Item g1-2 as shipped by g1 failed on its own control: the plan asserted `ls /tmp` stays refused, yet the CLI lists `/tmp` freely once `Bash(ls *)` is granted; and the oracle `"PROBE_OK_42" in stdout` matched the model's prose on a refused run (g1-2)
- Item g2-2 had no test behind it: g2 reported it as "driver-run … I did not run it" and shipped no `-m llm` case at all (g2-2)
- g6 and g8 edited `orchestrator/cli.py` and `orchestrator/execution/review.py`, outside their Files sets, as the wiring their units required (g6)
- g6-2's `-k "settle or approved"` filter deselected all 92 tests in the file; the coder ran the whole file and `tests/test_cli.py` instead, 211 passed (g6-2)
- g4 named the shared polling regex `_WAIT_RE`, while U8 imports a public `WAIT_COMMAND_RE` from a file g7 does not own (orchestrator/execution/round_signals.py)

## Run notes

Launched 11:58, last merge 13:02; one escalation, two hand commits during the run, two after it.

- Every group merged on its first round; g4 (difficulty `paired`) took one reviewer verdict, approved (g4/reviewer/gen1)
- 12:30, after g4 merged: committed the public alias `WAIT_COMMAND_RE = _WAIT_RE` on the integration branch (`8fef4ff`) so g7 forked with the name U8 imports (orchestrator/execution/round_signals.py)
- Escalation 898941bcac83 (`preflight_failed`, g7 gen1): fixed the g5 stub on the integration branch only (`467f0ef`, adds `peak_context_tokens=0`), verified 8/8 from g7's worktree against g7's code, answered `retry` with no text; the gate re-ran green and g7 merged through a refresh commit with no duplicate (898941bcac83)
- g1-2 run live three times: the shipped control failed; the test was rewritten to the same compound read with and without `--add-dir`, witnessed by a file the command creates; the final run shows the read refused without the flag ("outside this session's allowed working directory") and running with it, 31.8 s (g1-2)
- g2-2 written and run live through the real `SessionRunner` with Landlock on: `mktemp -d && echo ok` ran unrefused and made its directory under the worktree's `.coder-scratch/`, 6.4 s, no confinement warning (g2-2)
- Both live-test rewrites committed on the integration branch after the run ended, before `finish` (tests/test_read_roots.py)
- Scope probe of the read root, live through the confined runner with `--add-dir <run_dir>`: `ls <run_dir>` ran; `touch <run_dir>/x` failed with `Permission denied` (Landlock: the run dir is outside the write set); `touch /tmp/x` was refused by the CLI as outside the working directories — the flag widens reads of one directory and nothing else (tests/test_read_roots.py)

## Next steps

The orchestrator changes take effect on the next run, not this one; the plan-side lessons are about how verification items are written.

- Make a plan's negative control name the mechanism it isolates: "`/tmp` stays refused" measured the allowlist, not the read root; done when the deepen skill's `Pass:` guidance asks for the with/without-flag form (g1-2)
  - how: add the rule to `skills/orchestrator-deepen` beside the existing "Run (driver):" guidance
- Replace prose oracles in live tests with side effects: a model's answer quoting the marker passed a refused run; done when no `-m llm` test asserts on envelope text alone (tests/test_read_roots.py)
- Hold a merge whose driver-run item has no test behind it: g2 reported g2-2 without writing it; done when the "not run by the coder" check also confirms the named test exists and is merely deselected (g2-2)
- Name the wiring files in the plan: U6 and U9 touched `cli.py` and `review.py` unlisted; done when `plan-check` flags a unit whose Goal names a symbol living in a file outside its Files (orchestrator/execution/review.py)
- Use a real `SessionEntry` in the turn-observer tests instead of a `SimpleNamespace`, so the next field added to the per-turn bookkeeping cannot fail a sibling group's gate (tests/test_turn_observer.py)
- Watch the next run for the new anchors: `… at turn <t> —` lines from the ladder stop, repeat-denial stop, stall nudge and redundant-read reminder, and the `verification omitted after the last edit` gate line (orchestrator/execution/records.py)

<!-- valid pointers: 898941bcac83, 89faf361, 94652a2e, g1, g1-1, g1-2, g1-3, g1/coder/gen1, g2, g2-1, g2-2, g2-3, g2/coder/gen1, g3, g3-1, g3-2, g3-3, g3/coder/gen1, g4, g4-1, g4-2, g4-3, g4/coder/gen1, g4/reviewer/gen1, g5, g5-1, g5-2, g5-3, g5-4, g5/coder/gen1, g6, g6-1, g6-2, g6-3, g6/coder/gen1, g7, g7-1, g7-2, g7-3, g7-4, g7-5, g7-6, g7-7, g7-8, g7/coder/gen1, g8, g8-1, g8-2, g8-3, g8-4, g8/coder/gen1, orchestrator/cli.py, orchestrator/config.py, orchestrator/execution/escalating.py, orchestrator/execution/export.py, orchestrator/execution/generation.py, orchestrator/execution/prompting.py, orchestrator/execution/records.py, orchestrator/execution/review.py, orchestrator/execution/reviewer.py, orchestrator/execution/round_signals.py, orchestrator/execution/scheduler.py, orchestrator/execution/sessions.py, orchestrator/execution/streaming.py, orchestrator/execution/transcript_events.py, orchestrator/execution/worktrees.py, orchestrator/model.py, orchestrator/observatory/runs.py, orchestrator/prompts/limit_stop.md, orchestrator/prompts/redundant_read.md, orchestrator/prompts/repeat_denial.md, orchestrator/prompts/stall_nudge.md, orchestrator/prompts/worker_ground_rules.md, tests/test_cli.py, tests/test_content_filter.py, tests/test_export.py, tests/test_export_v3.py, tests/test_launch_env_rules.py, tests/test_read_roots.py, tests/test_review_loop.py, tests/test_review_scratch.py, tests/test_rewrite_counter.py, tests/test_round_signals.py, tests/test_streaming.py, tests/test_suspend_cure.py, tests/test_turn_observer.py, tests/test_verification_gate.py, tests/test_worktrees.py, u1, u2, u3, u4, u5, u6, u7, u8, u9 -->
