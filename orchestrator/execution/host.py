"""The seam a `_GroupExecution` mixin type-checks against (plan U1).

`_GroupExecution.__init__` is the only place any of this state is born; every
mixin composed onto it reads a subset via its own class-level annotations,
each name checked here — the mechanical guarantee `tests/test_execution_layout.py`
enforces so a second executor (Plan B) knows exactly what a mixin expects a
host to provide.

Every import here is `TYPE_CHECKING`-only: this module exists purely for
static annotations, and importing the concrete types at runtime would create
a cycle back through `review.py`, which imports mixins from the modules this
Protocol is meant to describe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from orchestrator.execution.heartbeat import RoundHeartbeat
    from orchestrator.execution.review import ReviewDeps
    from orchestrator.execution.scheduler import GroupContext
    from orchestrator.model import Group, SessionEntry


class ExecutionHost(Protocol):
    """Every attribute `_GroupExecution.__init__` sets, as data annotations
    only — no method is declared here as a Protocol attribute, since a mixin
    reading another mixin's method annotates that method itself (see
    `SurpriseHandling`'s `_log`/`_rewrite` annotations) rather than relying on
    this Protocol to carry it."""

    deps: ReviewDeps
    ctx: GroupContext
    group: Group
    gid: str
    generation: int
    rewrites: int
    sessions_spawned: int
    extra_pass_done: bool
    handoff_prompt: str | None
    workspace: Path | None
    coder_sid: str
    coder_entry: SessionEntry | None
    reviewer_sid: str | None
    _questions: int
    _grant_notes: list[str]
    _briefing_notes: list[str]
    _operator_notes: list[str]
    _env_failure: str | None
    _untracked_strikes: int
    _flake_reruns: int
    _reentry_entry: SessionEntry | None
    _heartbeat: RoundHeartbeat
    _cures: dict[int, int]
    _current_round_no: int

    # Cross-mixin methods a mixin may call on the host without defining
    # itself. Declared as `def` stubs, not variable annotations, so they are
    # absent from `ExecutionHost.__annotations__` — the data-attribute list
    # the layout test checks against `_GroupExecution.__init__`'s `vars()` —
    # while still resolving via `hasattr` for a mixin's own annotation of the
    # same name (e.g. `SurpriseHandling._log: Callable[[str], None]`).
    def _log(self, text: str) -> None: ...

    async def _rewrite(self, reason: str, *, extra: list[object] | None = None) -> None: ...

    async def _escalate(self, kind: object, **kwargs: object) -> object: ...

    async def _relaunch(self, note: str, why: str) -> None: ...

    def _persist_coder_usage(self) -> None: ...

    def _make_coder_on_turn(self, entry: object) -> object: ...

    def _spread(self, surprises: list[object]) -> None: ...

    def _round_tag(self, round_no: int) -> str: ...

    async def _worker_call(self, thunk: object, *, recover: object) -> object: ...
