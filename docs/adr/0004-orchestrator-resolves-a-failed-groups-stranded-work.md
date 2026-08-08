# ADR 0004 — The orchestrator commits and merges a failed group's stranded work

## Context

On run `r20260726-grouping`, g5 was marked `completed` having merged nothing: its
237 insertions across 5 files sat uncommitted in the worktree because the coder's
`git commit` was denied by the permission layer. The reviewer approved (it inspects
the working tree, not commits) and the merge silently no-op'd against a branch
byte-identical to the integration tip. Recovery was entirely manual.

## Decision

When a group ends in a Work Failure, the orchestrator may **commit whatever its
worktree still holds uncommitted and merge the branch** — autonomously when HITL is
off, or on operator request when HITL is on. The group lands in a new terminal
`RESOLVED` state, never `COMPLETED`. An Interrupted group is never resolved this way.

## Why

The orchestrator shells git directly (`worktrees.py:43`), outside the worker's
permission sandbox — which is exactly why an operator's `git commit` succeeded in
g7's worktree minutes after the coder's was denied. Recovering the work is therefore
mechanically available to the orchestrator whenever it is available to the operator.
`RESOLVED` is separate from `COMPLETED` because the merged work never passed review,
and a run report that conflates the two re-creates the failure this closes.

Rejected: merging only already-committed work — it cannot recover g5, the motivating
case. Rejected: re-running the group with a fresh coder — a full session's cost, and a
persistent denial simply reproduces the failure.
