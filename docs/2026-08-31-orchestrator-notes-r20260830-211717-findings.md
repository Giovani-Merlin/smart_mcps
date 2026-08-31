# Findings: the learning_podcast run notes (r20260830-211717) — 2026-08-31

Source: `learning_podcast/docs/orchestrator-notes.md` (10 items), checked
against the code and the run's own artifacts. Shipped as plugin **0.13.0**.

## What the evidence actually said

| note | verdict | detail |
| --- | --- | --- |
| #8 tail hidden | confirmed | `provision_env` logged `stderr[:500]` — the head. The `x Failed to build tts` line is at the tail. |
| #10 fake green | confirmed, worse | Provisioning ran `uv sync --all-extras` (failed on the `tts` extra); the merge gate ran plain `uv run pytest` — **no extras** — which built a core-only venv and passed 16 tests that lazily import the backends. `provisioning.json` said `failed` for all four groups; only the Observatory read it. |
| #2 recovery force-adds excluded files | **misdiagnosis** | `commit_all` is plain `git add -A`, which honours excludes. The repo's common `.git/info/exclude` held only the default comments; `git check-ignore der_sandmann.pdf` matched nothing. The files were simply untracked. Real bug: no size guard and no log of what got committed. `orchestrator/run-r20260830-211717` still carries the raw 143 MB blob (`e4b3517`) — do not merge it into master. |
| #3 refresh refused | consequence of #2 | untracked data files in g1's worktree collided with the same paths now tracked on integration. |
| #7 rebuilds per group | mostly a symptom of #8 | workers share one uv cache (`~/.cache/smart-mcps-orchestrator/uv`, not `~/.cache/uv`), and uv caches built wheels. Every group recompiled because the build *failed* — failures aren't cached. No seeded-venv mechanism needed; re-measure after a green sync. |
| #6 resume loses concurrency | half wrong | `resume` accepts `--concurrency`; what was missing is persistence of the launch value. |
| #4 zero-commit repo | confirmed + a second bug | `launch_commit_sha = _git(...).stdout.strip()` ignored the return code, so the baseline would be stamped with an empty sha. |
| #1, #5, #9 | confirmed gaps | nothing existed. |

## What shipped

- **Provisioning is fatal by default** — `session.provision_on_failure = "fail"`.
  `provision_env(strict=True)` raises `ProvisioningError` (a `WorktreeError`, so
  a group is INTERRUPTED not FAILED; `halt` admission stops the run). The
  integration worktree is provisioned at run start, so the podcast run would
  have stopped at launch with uv's real error. Logs carry the **tail** (1500
  chars + exit code). `"warn"` puts the failure into the coder's first prompt
  (`## Environment warning`).
- **Gate tests the provisioned environment** — `detect_check_steps(uv_run_args=…)`
  threads `provision_args` into `uv run <args> pytest`, for the merge gate and
  the baseline capture alike.
- **Shared data layer** — `[workspace] data_dirs`: symlinked into every worktree
  (group + integration, on create, re-entry, and after each merge), excluded
  via common `info/exclude`, granted under Landlock (`data_layer_write_paths`
  joins `extra_write_paths`). Bidirectional by construction. Landlock never
  restricts reads, so the note's hardlink worry was moot.
- **Large-file safety net** — `relocate_large_files` runs inside `commit_all`
  when a workspace is given: untracked files ≥ `large_file_bytes` are moved to
  `.orchestrator/data/<relpath>`, symlinked back, excluded, registered
  (`.registry.json`), and linked into later worktrees. `commit_all` now logs
  what it committed.
- **`resume` restores the launch `ExecutionConfig`** — persisted on
  `RunManifest.execution`, slotted into `_load_config` under the CLI flags.
- **HEAD precondition** for a fresh run; **`status`** with no id shows the single
  unfinished run (else the newest) and lists the rest; several unfinished →
  the list.
- **Plan skill** — verifier check 9: inputs the plan names must be tracked or
  under a configured data dir (ADVISORY).
- Worker ground rules mention the data layer and the size cap.

## Deliberately not done

- Seeded per-group venv copies (#7): not needed while the shared cache holds
  built wheels; revisit only if a green run still rebuilds.
- Auto-deleting untracked files that block a refresh (#3): the data layer
  removes the cause; the generic case is still "resolve by hand, then `retry`".

## Follow-ups worth a look

- The gate's `uv run` and the group's `uv sync` now agree on extras, but a
  configured `preflight.check_command` is passed through verbatim — an operator
  who sets `["uv","run","pytest"]` opts out of the extras alignment.
- `ensure_excluded` writes the *common* exclude file, so `/data` is also
  excluded in the operator's main checkout. Intended (data never goes in git),
  but worth knowing when a repo actually wants to commit something there.
