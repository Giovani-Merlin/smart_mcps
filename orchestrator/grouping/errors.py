"""Shared multi-error accumulation for grouping validation phases (plan U6).

Grouping validation runs in phases — slice-overflow checking, task-map shape,
task-map cross-references — and each phase used to raise on the first problem
it found. On a written plan with several independent problems in one phase,
that cost one `group` invocation per problem (C1/R5) instead of one per phase.
``ErrorAccumulator`` collects every problem a phase finds and raises them
together; phase order stays the caller's responsibility — call ``raise_all``
at the end of one phase before starting the next, so a later phase never runs
against input a still-failing earlier phase already condemned.
"""

from __future__ import annotations


class ErrorAccumulator:
    """Collects problem messages for one validation phase.

    A single accumulated message raises with its own wording unchanged, so
    single-error call sites keep their existing exact text; two or more join
    into one report listing each verbatim.
    """

    def __init__(self) -> None:
        self._messages: list[str] = []

    def add(self, message: str) -> None:
        self._messages.append(message)

    def __bool__(self) -> bool:
        return bool(self._messages)

    def __len__(self) -> int:
        return len(self._messages)

    def raise_all(self, exc_type: type[Exception]) -> None:
        """Raise ``exc_type`` with every accumulated message, or do nothing."""
        if not self._messages:
            return
        if len(self._messages) == 1:
            raise exc_type(self._messages[0])
        detail = "\n".join(f"  - {message}" for message in self._messages)
        raise exc_type(f"{len(self._messages)} problems found:\n{detail}")
