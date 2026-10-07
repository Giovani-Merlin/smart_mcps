## 2026-10-07 — r20261007-082701 — Fixes from run r20261006-162245 — export honesty, result-aware liveness, run phase lines, own-group surprises, the live-tier guard, and two plan-check seeds

- **Outcome**: 8/8 groups completed, 8/8 units landed (`state.json`)
- **Scope**: 25 files changed, +1035/-71 lines (`78b4232a..aa6d8fde`)
- **Cost**: 659703 tokens (+5664215 cache-read) across 9 session(s) (sonnet=659703) (`manifest.json`)

### g1: export-honesty — export prints N/M sessions transcript_missing, refuses a metadata-only bundle without --allow-missing, refuses a non-empty --out without --clear — state: completed
- **Summary**: export now computes a transcript census before writing, refuses (exit 1, nothing written) when any session is transcript_missing unless --allow-missing,… (`g1`)
- **Verification**: 2/2 pass (`g1`)
- **Surprises**: none recorded (`g1`)
- **Required changes**: none (`g1`)
- **Escalations**: none (`g1`)
- **Tokens**: 99592 tokens (+1045160 cache-read) across 1 session(s) (sonnet=99592) (`g1`)
- **Elapsed**: 5m (`g1`)

| item | status | evidence |
| --- | --- | --- |
| g1-1 | pass | tests/test_export_honesty.py green; with test_export.py, test_export_v3.py and test_cli.py 156 passed |
| g1-2 | pass | CLI on fixture r20260828-220035 printed '0/11 sessions with transcripts, 11 transcript_missing', exit 1, no ingest.json written |

### g2: result-aware-liveness — CPU ticks alone are not a Sign of Life after the child's last event is a result — state: completed
- **Summary**: sign_of_life now skips the CPU signal when the child's last_event_type is 'result', returning signal None with evidence 'idle after result'… (`g2`)
- **Verification**: 3/3 pass (`g2`)
- **Surprises**: none recorded (`g2`)
- **Required changes**: none (`g2`)
- **Escalations**: none (`g2`)
- **Tokens**: 60709 tokens (+352399 cache-read) across 1 session(s) (sonnet=60709) (`g2`)
- **Elapsed**: 4m (`g2`)

| item | status | evidence |
| --- | --- | --- |
| g2-1 | pass | 2 passed against a real busy child and real /proc |
| g2-2 | pass | 78 passed across test_liveness, test_liveness_wiring and test_heartbeat |
| g2-3 | pass | CONTEXT.md line 292 matches 'not evidence' |

### g3: run-phase-lines — every Run Child command writes "command n/total: exit k (Ns)" to run.log — state: completed
- **Summary**: Added _log_command and an attempt-start log line in run_executor.py (run.log gets 'command n/total: exit k (N.Ns)' per command, including the… (`g3`)
- **Verification**: 1/2 pass (`g3`)
- **Surprises**: none recorded (`g3`)
- **Required changes**: none (`g3`)
- **Escalations**: none (`g3`)
- **Tokens**: 73053 tokens (+760050 cache-read) across 1 session(s) (sonnet=73053) (`g3`)
- **Elapsed**: 5m (`g3`)

| item | status | evidence |
| --- | --- | --- |
| g3-1 | pass | uv run pytest tests/test_run_phase_lines.py -q: 2 passed; tests/test_run_executor.py also green (25 passed alongside) |
| g3-2 | driver-run | driver-run: the item uses -m llm, which spawns nested claude, so I did not run it |

### g4: own-group-surprise — a surprise naming only its own group is dropped with a log line, never bucketed as "__run__ — unknown group id" — state: completed
- **Summary**: SurpriseBoard.mark now drops a surprise whose every target resolves to its own source group, logging the own-group anchor line and… (`g4`)
- **Verification**: 3/3 pass (`g4`)
- **Surprises**: none recorded (`g4`)
- **Required changes**: none (`g4`)
- **Escalations**: none (`g4`)
- **Tokens**: 70131 tokens (+526842 cache-read) across 1 session(s) (sonnet=70131) (`g4`)
- **Elapsed**: 4m (`g4`)

| item | status | evidence |
| --- | --- | --- |
| g4-1 | pass | tests/test_surprise_own_group.py green (5 tests) |
| g4-2 | pass | test_surprise_board, test_informational_surprise, test_finish green after updating one test that pinned the superseded own-task-to-__run__ behaviour |
| g4-3 | pass | printed '__run__: 1 pending — no target group named, or an unknown id' |

### g5: live-tier-guard — the streaming live suite gains a two-follow-up termination test — state: completed
- **Summary**: Added test_two_followups_mid_turn_still_terminate to tests/test_streaming_live.py (two back-to-back send() calls from the first on_turn; asserts termination, returncode 0, envelope present; no folding/result-count… (`g5`)
- **Verification**: 1/2 pass (`g5`)
- **Surprises**: none recorded (`g5`)
- **Required changes**: none (`g5`)
- **Escalations**: none (`g5`)
- **Tokens**: 56531 tokens (+212542 cache-read) across 1 session(s) (sonnet=56531) (`g5`)
- **Elapsed**: 3m (`g5`)

| item | status | evidence |
| --- | --- | --- |
| g5-1 | pass | collect-only listed 5 tests including test_two_followups_mid_turn_still_terminate |
| g5-2 | driver-run | driver-run |

### g6: named-test-hold — a required driver-run item naming a pytest path absent from the worktree is a gap at the report gate — state: completed
- **Summary**: Added orchestrator/execution/driver_items.py with driver_items_naming_missing_tests, wired it into _settle_round (code groups only) right after unmet_required_verification, and added tests/test_named_test_hold.py including a Harness-based… (`g6`)
- **Verification**: 3/3 pass (`g6`)
- **Surprises**: none recorded (`g6`)
- **Required changes**: none (`g6`)
- **Escalation (preflight_failed)**: retry (`escalations/request-9f5d036ceabc.json`)
- **Tokens**: 76426 tokens (+751085 cache-read) across 1 session(s) (sonnet=76426) (`g6`)
- **Elapsed**: 9m (`g6`)

| item | status | evidence |
| --- | --- | --- |
| g6-1 | pass | tests/test_named_test_hold.py green, includes Harness path to required_changes |
| g6-2 | pass | test_driver_run_items.py and test_merge.py green (32 passed with the new file) |
| g6-3 | pass | ran it: printed a one-element list naming tests/test_nope.py; no nested claude, no writes outside the sandbox |

### g7: goal-symbol-lint — plan-check warns when a unit's Goal changes a symbol defined in a file outside that unit's Files — state: completed
- **Summary**: Added lint_goal_symbols to orchestrator/grouping/verification_lint.py (subject-verb backticked-name detection, definitions index via git ls-files with rglob fallback, warn when defining files are… (`g7`)
- **Verification**: 3/3 pass (`g7`)
- **Surprises**: none recorded (`g7`)
- **Required changes**: none (`g7`)
- **Escalation (preflight_failed)**: retry (`escalations/request-1b8bf51444af.json`)
- **Tokens**: 84553 tokens (+803514 cache-read) across 1 session(s) (sonnet=84553) (`g7`)
- **Elapsed**: 9m (`g7`)

| item | status | evidence |
| --- | --- | --- |
| g7-1 | pass | tests/test_goal_symbol_lint.py green (6 cases); test_verification_lint.py still green |
| g7-2 | pass | mutated plan yields warning naming export_run and orchestrator/execution/export.py, exit 0 |
| g7-3 | pass | real plan: exit 0, no 'Goal changes' warning |

### g8: docs-sweep — every skill, contract and grouping-doc line the seven fixes introduce, in one pass — state: completed
- **Summary**: Joined the split f-string in run_executor._log_command so ': exit ' is contiguous in source (the emitted line is byte-identical). g8-5… (`g8`)
- **Verification**: 5/5 pass (`g8`)
- **Verdict**: approved (`g8`)
- **Surprise (other)**: g8-5's assertion `': exit ' in src` cannot pass against the merged run_executor.py, because the phase-line f-string is split across two literals (lines 215-217). Either join the literal in _log_command or rewrite the item to check the emitted line. (`groups/g8/report-g1-r1.json`)
- **Required change**: [g8-5] reported fail: Run: `uv run python -c "import re,pathlib,subprocess; src=pathlib.Path('orchestrator/execution/run_executor.py').read_text(); doc=pathlib.Path('skills/orchestrator-run/SKILL.md').read_text(); assert 'starting at command' in src and 'starting at command' in doc; assert ': exit ' in src and ': exit <k>' in doc; print('ok')"` Pass: prints `ok` — the documented anchor strings are the ones the merged code writes. — AssertionError on `': exit ' in src`. 'starting at command' is in both the code and the doc. The code writes `command {n}/{total}: ` and `exit {status} ({N.N}s)` as separate f-string literals, so the contiguous check cannot match even though the emitted line is 'command n/total: exit k (N.Ns)', which matches the doc. The item's check is wrong for this code layout; fixing it needs a change to run_executor.py (outside this unit's Files) or to the item. (`groups/g8/verdict-g1-r1.json`)
- **Escalations**: none (`g8`)
- **Tokens**: 138708 tokens (+1212623 cache-read) across 2 session(s) (sonnet=138708) (`g8`)
- **Elapsed**: 6m (`g8`)

| item | status | evidence |
| --- | --- | --- |
| g8-1 | pass | allow-missing and --clear present in both files |
| g8-2 | pass | all six strings present |
| g8-3 | pass | Existence paragraph updated; 'only at finish' count 0 |
| g8-4 | pass | all three strings present |
| g8-5 | pass | prints ok after joining the f-string literal in run_executor.py |

## Diagrams

### Plan → outcome

```mermaid
flowchart LR
    classDef ok fill:#d1f5d3,stroke:#2f9e44,color:#1a1a1a;
    classDef fail fill:#ffd6d6,stroke:#c92a2a,color:#1a1a1a;
    classDef resolved fill:#fff3bf,stroke:#e8a400,color:#1a1a1a;
    u_u1["u1- export-honesty — export prints N/M sessions transcript_missing; refuses a me"]
    grp_g1["g1- export-honesty — export prints N/M sessions transcript_missing; refuses a metadata-only bundle without --allow-missing; refuses a non-empty --out without --clear"]
    u_u1 --> grp_g1
    u_u2["u2- result-aware-liveness — CPU ticks alone are not a Sign of Life after the chi"]
    grp_g2["g2- result-aware-liveness — CPU ticks alone are not a Sign of Life after the child's last event is a result"]
    u_u2 --> grp_g2
    u_u3["u3- run-phase-lines — every Run Child command writes "command n/total- exit k (N"]
    grp_g3["g3- run-phase-lines — every Run Child command writes "command n/total- exit k (Ns)" to run.log"]
    u_u3 --> grp_g3
    u_u4["u4- own-group-surprise — a surprise naming only its own group is dropped with a "]
    grp_g4["g4- own-group-surprise — a surprise naming only its own group is dropped with a log line; never bucketed as "__run__ — unknown group id""]
    u_u4 --> grp_g4
    u_u5["u5- live-tier-guard — the streaming live suite gains a two-follow-up termination"]
    grp_g5["g5- live-tier-guard — the streaming live suite gains a two-follow-up termination test"]
    u_u5 --> grp_g5
    u_u6["u6- named-test-hold — a required driver-run item naming a pytest path absent fro"]
    grp_g6["g6- named-test-hold — a required driver-run item naming a pytest path absent from the worktree is a gap at the report gate"]
    u_u6 --> grp_g6
    u_u7["u7- goal-symbol-lint — plan-check warns when a unit's Goal changes a symbol defi"]
    grp_g7["g7- goal-symbol-lint — plan-check warns when a unit's Goal changes a symbol defined in a file outside that unit's Files"]
    u_u7 --> grp_g7
    u_u8["u8- docs-sweep — every skill; contract and grouping-doc line the seven fixes int"]
    grp_g8["g8- docs-sweep — every skill; contract and grouping-doc line the seven fixes introduce; in one pass"]
    u_u8 -->|approved| grp_g8
    grp_g1:::ok
    grp_g2:::ok
    grp_g3:::ok
    grp_g4:::ok
    grp_g5:::ok
    grp_g6:::ok
    grp_g7:::ok
    grp_g8:::ok
```

## ADR delta

- **ADR delta**: no ADR changes (`78b4232a..aa6d8fde`)

## Postmortem

### Impact

- **Impact**: every unit landed despite the trouble below (`r20261007-082701`)

### Timeline

- **session_start**: coder at 2026-10-07T06:30:36.378164+00:00 (`g1`)
- **session_end**: coder at 2026-10-07T06:35:50.504+00:00 (`g1`)
- **session_start**: coder at 2026-10-07T06:35:52.977922+00:00 (`g2`)
- **session_end**: coder at 2026-10-07T06:40:06.876+00:00 (`g2`)
- **session_start**: coder at 2026-10-07T06:40:09.635511+00:00 (`g3`)
- **session_end**: coder at 2026-10-07T06:45:50.884+00:00 (`g3`)

### Root-cause candidates

- **Required change (g8)**: [g8-5] reported fail: Run: `uv run python -c "import re,pathlib,subprocess; src=pathlib.Path('orchestrator/execution/run_executor.py').read_text(); doc=pathlib.Path('skills/orchestrator-run/SKILL.md').read_text(); assert 'starting at command' in src and 'starting at command' in doc; assert ': exit ' in src and ': exit <k>' in doc; print('ok')"` Pass: prints `ok` — the documented anchor strings are the ones the merged code writes. — AssertionError on `': exit ' in src`. 'starting at command' is in both the code and the doc. The code writes `command {n}/{total}: ` and `exit {status} ({N.N}s)` as separate f-string literals, so the contiguous check cannot match even though the emitted line is 'command n/total: exit k (N.Ns)', which matches the doc. The item's check is wrong for this code layout; fixing it needs a change to run_executor.py (outside this unit's Files) or to the item. (`groups/g8/verdict-g1-r1.json`)
- **Surprise (g8, other)**: g8-5's assertion `': exit ' in src` cannot pass against the merged run_executor.py, because the phase-line f-string is split across two literals (lines 215-217). Either join the literal in _log_command or rewrite the item to check the emitted line. (`groups/g8/report-g1-r1.json`)

### Follow-ups

- **Open required change (g8)**: [g8-5] reported fail: Run: `uv run python -c "import re,pathlib,subprocess; src=pathlib.Path('orchestrator/execution/run_executor.py').read_text(); doc=pathlib.Path('skills/orchestrator-run/SKILL.md').read_text(); assert 'starting at command' in src and 'starting at command' in doc; assert ': exit ' in src and ': exit <k>' in doc; print('ok')"` Pass: prints `ok` — the documented anchor strings are the ones the merged code writes. — AssertionError on `': exit ' in src`. 'starting at command' is in both the code and the doc. The code writes `command {n}/{total}: ` and `exit {status} ({N.N}s)` as separate f-string literals, so the contiguous check cannot match even though the emitted line is 'command n/total: exit k (N.Ns)', which matches the doc. The item's check is wrong for this code layout; fixing it needs a change to run_executor.py (outside this unit's Files) or to the item. (`groups/g8/verdict-g1-r1.json`)
