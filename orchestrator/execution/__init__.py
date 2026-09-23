"""Execution engine: sessions, worktrees, manifest, scheduler, review, merge (plan Phase B).

A group's execution (plan U1-U5) splits across six mixins composed onto
`review._GroupExecution`: `generation.py` (the generation lifecycle),
`reviewer.py` (the reviewer round), `records.py` (session records and
usage), `merge_ladder.py` (the merge gate), `escalating.py` (escalation,
rewrite, relaunch) and `surprises.py` (the cross-group `SurpriseBoard`).
`host.py` declares the `ExecutionHost` Protocol every mixin reads from.
"""
