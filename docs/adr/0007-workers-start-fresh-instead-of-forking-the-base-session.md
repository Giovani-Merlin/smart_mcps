# 0007 — Workers start fresh instead of forking the base session

**Context.** Every worker session was launched by forking the run's one base
session (`claude -p --resume <base> --fork-session`). The premise: compile an
expensive base context once — worker ground rules, repo conventions, the
codegraph architecture summary, the plan digest — pay for it in a single Opus
call, and let each of the run's ~2N worker launches inherit it from the prompt
cache for nearly nothing. `compile_base_context` is byte-stable specifically so
that shared prefix stays cache-identical.

Measured on r20260828-090936 (`scripts/measure_fork_cache.py`), the premise does
not hold. **Every** fork hits exactly **19,968 cached tokens** and re-creates
the remaining **~41.5k** of the base context as fresh prefix. The cause is
structural, not tunable: the prompt-cache key begins with the system prompt,
which embeds the working directory and a git snapshot of it, and each group's
cwd is its own worktree. The prefix diverges at the first cwd-dependent byte,
and everything after it is paid for again. Arm B of the same experiment —
forking with cwd set to the repo root and handing the worktree over via
`--add-dir` — was a dead end on both counts: it won no additional cache and it
committed the worker to operating outside its own worktree.

**Decision.** `SessionRunner.start_worker` is the launch path: a fresh session
whose first prompt is the base context followed by the worker's own rendered
prompt. `session.fork_base_session` (default `false`, CLI
`--fork-base`/`--no-fork-base`) keeps the fork reachable. With it off, the run
spawns no base session at all and `manifest.base_session_id` is `None`.

**Why.** Be straight about the size of the win: the fork miss is roughly **3% of
input-side spend**, and a fresh session pays that same base context as input —
exactly what the fork re-creates anyway. This is a simplification, not a cost
cut. What it actually removes is real, though: the run's one Opus `start_base`
call (`base_model` defaults to `claude-opus-5`), the serialized `_fork_lock`
every launch queued behind, a whole launch step with its own failure modes and
its own multi-minute silence before any group started, and the transcript replay
that put the parent session's records verbatim at the head of every worker's
jsonl — the trap that produced a wrong first reading of the fork measurement and
is the reason `measure_fork_cache.py` matches records by uuid at all.

It is worth doing because the mechanism it pays for demonstrably does not work.

**Why the code is kept, not deleted.** The premise is sound; only the current
implementation of the cache key falsifies it. `start_fork` and
`_fork_cwd_experiment` stay byte-for-byte behind the flag, carrying `LEGACY`
docstrings that name the measurement and its date. The day a Claude Code release
stops keying the cache prefix on cwd, flipping the default back is the whole
change — so `tests/test_e2e_stub.py` keeps an explicit `--fork-base` run
asserting the old launch shape end to end, and `--resume` / `--fork-session`
stay in `REQUIRED_CLI_FLAGS`: `resume` still uses the former, and preflight
should keep failing loudly if a CLI drops either.
