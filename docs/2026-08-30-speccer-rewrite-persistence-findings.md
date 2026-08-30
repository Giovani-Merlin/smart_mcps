# The rewrite speccer's new name never reaches `groups.json` — run r20260830-163212

Written 2026-08-30, from resuming `r20260830-163212` in `drummAI` (plan
`docs/plans/2026-08-29-001-docs-pipeline-deep-dive-plan.md`). §1 is a P0 that
makes any speccer-rewritten group unresumable and leaks its worktree at
teardown; §2 is the one-line-ish fix seam, which already exists and is simply
not read; §3 is the operator repair that unblocked this run; §4 is an audit-trail
bug that nearly destroyed the evidence; §5 is a live environment bug still open
for g2/g4/g5.

Every claim here was observed on the run, not reasoned from the source. It fired
**twice** during the writing of this document — g1 at 18:46 and g4 at 19:22 —
so this is a per-rewrite certainty, not a race.

______________________________________________________________________

## 1. P0 — a rewritten group desyncs from `groups.json`

`resume r20260830-163212` died in under a second, exit 2:

```
g1: interrupted (generation 2) — WorktreeError: git worktree add
  .worktrees/r20260830-163212/g1-registry-one-resolver-every-chapter-read
  orchestrator/r20260830-163212-g1 failed:
fatal: 'orchestrator/r20260830-163212-g1' is already used by worktree at
       '.worktrees/r20260830-163212/g1-registry-cache-notation-slice'
```

Two slugs for one group, one branch. The chain:

- `orchestrator/cli.py:2473` `_rewrite_provider.rewrite_spec` ends at
  `cli.py:2487` with `return group.model_copy(update={"name": spec.name, …})` —
  a **new in-memory `Group`**. Nothing updates `groups.json`.
- `orchestrator/execution/worktrees.py:141` `worktree_path` slugs the directory
  from that name (`:150`), and `cli.py:2203` `workspace_for` passes
  `name=group.name` (`:2210`).

So the worktree lands under the **speccer** name while `groups.json` keeps the
**grouper** name, and every restart path re-derives the stale one:

| Reader                              | Site                    | Consequence                                                                                                                          |
| ----------------------------------- | ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `workspace_for` → `create_worktree` | `cli.py:2203`, `:2206`  | `worktree add` onto a branch already checked out elsewhere → the error above. **Hard-fails the resume.**                             |
| `_group_name` → `_teardown_group`   | `finish.py:327`, `:342` | `existing_worktree_path` returns `None`; teardown silently no-ops and leaks the worktree. **Latent, not live** — see the note below. |
| `_group_name`                       | `retry.py:44`, `:103`   | `retry` points at a path that does not exist.                                                                                        |

**Correction, from watching g4 finish.** An earlier draft of this document
claimed the teardown leak bites on a clean run. It does not, and the run
disproved it: g4 merged with a desynced name and its worktree was still removed
cleanly (`.worktrees/` left holding only `integration`). The merge-time teardown
runs **in-process**, where the driver still holds the rewritten `Group`, so the
correct name is in hand and `groups.json` is never consulted.

The `finish.py` path is therefore **latent**: it only reads `groups.json` when
`finish` runs as a *separate invocation*, and by then a merged group's worktree
is already gone, so `_teardown_group` no-ops for the right reason. It becomes
real exactly when a rewritten group's worktree outlives the driver — worktrees
are not removed on interrupt (`create_worktree` docstring), and a
failed/quarantined group keeps its worktree too. Then a later `finish` or
`retry` cannot find it. Same root cause, narrower blast radius than §1's table
alone suggests.

Observed both times:

| Group | `groups.json`                                                         | worktree on disk                   |
| ----- | --------------------------------------------------------------------- | ---------------------------------- |
| g1    | `registry — one resolver every chapter reads its subject through + …` | `g1-registry-cache-notation-slice` |
| g4    | `front-end — Chapter 01: what each backend imposes on the audio + …`  | `g4-frontend-chapters-01-02`       |

`manifest.json` is *not* stale — `record_session` (`manifest.py:313`) is fed
`group.name` off the live object (`prompting.py:121`), so it carries the speccer
name. The two files disagree and the one every restart path trusts is wrong.

**The existing adoption path cannot rescue this.** `create_worktree`
(`worktrees.py:227`) adopts a stale worktree via `git worktree move`, but
`_legacy_worktree_path` (`:154`) models only the pre-U2 *layout* change — same
name, different parent. A **name** change under the same layout is not a case it
recognises, so it falls through to `worktree add` and dies.

______________________________________________________________________

## 2. The fix seam already exists — nothing reads it

`review.py:1339` writes the complete rewritten `Group` to
`groups/<gid>/spec-gen<N>.json`:

```python
path = self.deps.store.paths.group_dir(self.gid) / f"spec-gen{self.generation}.json"
```

Verified: `groups/g1/spec-gen1.json` (18:46) and `groups/g4/spec-gen1.json`
(19:22) are full group objects — `id`, `name`, `summary`, `spec`, `difficulty`,
`intensity`, `dependencies`, `verification`, `tasks`, `files`,
`estimated_tokens` — carrying the speccer names. **The rewrite is durably
persisted. The restart paths just don't look.**

The only reader outside the writer is the observatory
(`observatory/runs.py:384-402`, `_SPEC_GEN_RE`). Grep confirms no other
consumer.

So the fix is narrower than "persist the rewrite":

1. **Preferred — make the restart paths generation-aware.** Have
   `workspace_for`, `finish._group_name` and `retry._group_name` resolve a
   group as *highest-N `spec-gen<N>.json`, else `groups.json`*. One shared
   helper; `groups.json` stays the immutable grouper output, which is probably
   the right layering.
2. **Or** have `rewrite_spec` write back to `groups.json` too. Simpler, but it
   makes `groups.json` mutable mid-run and loses the generation history that
   `spec-gen<N>` gives you.
3. Independently, prefer `existing_worktree_path`/`_registered_branch` over a
   bare `worktree add` so the error names the real directory instead of dying.

Regression test: rewrite a group mid-run, kill the driver, resume, assert the
same worktree is reused; then `finish` and assert it is torn down.

______________________________________________________________________

## 3. The operator repair that unblocked this run

Restored `name`, `summary`, `spec`, `verification` into g1 in `groups.json`
(backup `groups.json.bak-20260830T191758`):

| Field          | Before (grouper)                                                      | After (speccer)                 |
| -------------- | --------------------------------------------------------------------- | ------------------------------- |
| `name`         | `registry — one resolver every chapter reads its subject through + …` | `registry-cache-notation-slice` |
| `spec`         | 26508 chars                                                           | 4498 chars                      |
| `verification` | 34 items                                                              | 16 items                        |

Afterwards `groups/g1/spec-gen1.json == groups.json[g1]` on the **full object**,
which confirms the repair reproduced the authoritative record exactly.

**Restore all four fields, not just `name`.** Name-only resumes the run but
leaves the pre-rewrite `verification`, and the merge gate from `cbc99e5` would
hold the group to 34 items it never agreed to — including `uv sync` steps §5
made impossible. The rewrite cut it to 16, among them `g1-no-uv-sync`. Live
proof the repair was sufficient: g1 went `reviewing → merging → completed`, and
its worktree was torn down cleanly *because* the names matched.

**Recover from `groups/<gid>/spec-gen<N>.json`, not from `llm/`** — see §4.

______________________________________________________________________

## 4. The rewrite audit trail is overwritten, not appended

`llm/01-speccer_output-a0.raw.txt`, `…request.txt` and `llm/calls.json` are
written at **fixed names**, so each rewrite clobbers the last:

- 18:46 — the file held g1's payload (`"name":"registry-cache-notation-slice"`).
- 19:22 — same filename, now g4's payload (`"name":"frontend-chapters-01-02"`).
- `calls.json` still reports `calls: 1`, `seq: 1`, its single entry re-stamped
  to the g4 call.

g1's rewrite record was destroyed. It was recoverable only because
`spec-gen1.json` exists — had the repair in §3 relied on `llm/` and run 5
minutes later, g1's spec would have been gone. Mapper calls append; rewrite
calls should too (`seq`-prefixed filenames, `calls` appended).

______________________________________________________________________

## 5. OPEN — EXDEV in the worker worktrees blocks any real `uv` install

Recorded as surprises by the g2 coder and its reviewer,
`affected_groups: [g1, g2, g4, g5]`, reproduced three ways by the worker:

> Renaming a freshly-created file into a sibling directory fails with
> `OSError EXDEV ('Invalid cross-device link')`, even when source and
> destination report the identical `st_dev` and even with
> `dangerouslyDisableSandbox`. Reproduced with plain Python `os.rename` (no uv
> involved), across the shared uv cache, a custom cache dir inside the worktree,
> and `/tmp` with `--no-cache`.

Any `uv sync` / `uv pip install` / `uv build` needing an isolated build env
(e.g. building drummai's wheel via hatchling) fails unconditionally there. The
worker fell back to plain `pip` plus `uv run --no-sync`, which matches this
project's standing convention anyway.

Two things to carry forward:

- **Same class as `worktrees.py:255`**, where `create_worktree` already catches
  `cross-device` from `git worktree move` and falls back to `shutil.move` +
  `git worktree repair`. The provisioning path has no equivalent fallback.
  Worth checking whether landlock (abi 3) confinement is what makes
  `st_dev`-identical renames fail.
- **This surprise is what triggers the rewrite.** Delivery on admission
  synthesises the rewrite context, so g4 rewrote the moment it was admitted, and
  g5 and g2 carry the same undelivered surprises. §1 therefore fires for every
  remaining group in this run — it is the common path here, not an edge case.

______________________________________________________________________

## 6. State to pick up from

Run `r20260830-163212`, concurrency 1, on-failure halt:
g1, g3, g4 completed and merged (g4 rewritten and torn down cleanly despite the
desync) · g5 rewriting · g2 pending on g1/g4/g5.

Branches `orchestrator/r20260830-163212-{g1,g3,g4}` still exist, which is normal
pre-`finish`. No reconciliation is needed while the run completes in one driver
process. It *is* needed before a `finish` or `retry` that follows an interrupt
or a failed group — copy `name`/`summary`/`spec`/`verification` from the highest
`groups/<gid>/spec-gen<N>.json` into `groups.json` first.
