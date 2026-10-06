"""Session records: manifest entries, per-session usage and transcript bookkeeping (plan U4).

`SessionRecords` is a mixin with no `__init__`: every attribute it reads is
declared below and born in the composition root (`ExecutionHost`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator.execution.heartbeat import RoundHeartbeat
from orchestrator.execution.manifest import record_session
from orchestrator.execution.prompting import (
    render_ladder_compact_prompt,
    render_ladder_prioritized_prompt,
    render_ladder_summary_prompt,
    render_limit_stop_prompt,
    render_redundant_read_prompt,
    render_repeat_denial_prompt,
    render_stall_nudge_prompt,
)
from orchestrator.execution.round_signals import READ, RoundSignals
from orchestrator.execution.sessions import RoundResult, SessionRunner, session_display_name
from orchestrator.execution.streaming import TurnUsage
from orchestrator.model import Group, SessionEntry, SessionRole


if TYPE_CHECKING:
    from orchestrator.execution.review import ReviewDeps


class SessionRecords:
    deps: ReviewDeps
    gid: str
    generation: int
    workspace: Path | None
    coder_sid: str
    coder_entry: SessionEntry | None
    sessions_spawned: int
    _current_round_no: int
    _heartbeat: RoundHeartbeat
    _log: Callable[[str], None]

    def _copy_usage(self, entry: SessionEntry, session_id: str) -> None:
        """Mirror the in-memory cumulative usage onto a manifest entry.

        ``last_context_tokens`` is the breaker's input (occupancy of the latest
        round); the cumulative counters are the session's total spend, kept split
        by token class so an estimate-vs-actual view can tell a cache-heavy run
        from an genuinely expensive one. Both are written on the same save.
        """
        usage = self.deps.runner.usage_of(session_id)
        entry.last_context_tokens = usage.last_context_tokens
        entry.rounds_completed = usage.rounds
        entry.total_input_tokens = usage.total_input_tokens
        entry.total_output_tokens = usage.total_output_tokens
        entry.total_cache_read_tokens = usage.total_cache_read_tokens
        entry.total_cache_creation_tokens = usage.total_cache_creation_tokens
        entry.base_context_tokens = usage.base_context_tokens
        entry.total_cost_usd = usage.total_cost_usd

    def _persist_coder_usage(self) -> None:
        """Record the active coder's latest context size on its manifest entry
        after every round (R5): in-memory usage dies with the process, and the
        next re-entry pre-checks this value against the breaker limit."""
        if self.coder_entry is None:
            return
        self._copy_usage(self.coder_entry, self.coder_sid)
        self.deps.store.save(self.deps.manifest)

    def _make_coder_on_turn(
        self, entry: SessionEntry
    ) -> Callable[[TurnUsage, Callable[[str], None], Callable[[str], None] | None], None]:
        """Per-turn observer for one coder round call (plan U1's seam).

        - continuous bookkeeping (plan U4): ``last_context_tokens`` updates as
          turns stream in, not only once the round finishes and
          ``_persist_coder_usage`` runs.
        - the staged context ladder (plan U3), gated by
          ``breaker.context_ladder_enabled`` (on by default): 70%/90%/100% of
          ``context_token_limit`` each send at most one staged prompt onto the
          still-running round via ``send``; at 100% the round is also ended
          (``end_round``) after the compact prompt, so the limit holds within one
          turn. ``_breaker_reason`` still retires the group at the boundary.
        - the round's ``RoundSignals`` (exposed as ``on_turn.signals``; the
          session runner installs it as the stream's ``on_tool_event`` and
          attaches it to the ``RoundResult``): the third identical denied command
          ends the round, a stall window earns one "change approach" nudge, and a
          redundantly re-read file earns one reminder. They share the ladder
          switch: off disables every in-round prompt.

        All state is local to this call, so a fresh round starts clean.
        """
        fired: set[str] = set()
        breaker = self.deps.breaker
        signals = RoundSignals(stall_k=breaker.stall_window_k)
        denial_ended: set[str] = set()
        stall_nudged: dict[int, int] = {}
        reminded: set[str] = set()

        def on_turn(
            usage: TurnUsage,
            send: Callable[[str], None],
            end_round: Callable[[str], None] | None = None,
        ) -> None:
            end = end_round or send
            turn = signals.turn_index
            context = (
                usage.input_tokens
                + usage.output_tokens
                + usage.cache_read_input_tokens
                + usage.cache_creation_input_tokens
            )
            entry.last_context_tokens = context
            self.deps.store.save(self.deps.manifest)
            if not breaker.context_ladder_enabled:
                return

            def note(signal: str, detail: str) -> None:
                self._log(
                    f"group {self.gid} generation {self.generation}: "
                    f"{signal} at turn {turn} — {detail}"
                )

            limit = breaker.context_token_limit
            if context >= limit and "100" not in fired:
                fired.update({"70", "90", "100"})
                send(render_ladder_compact_prompt())
                end(render_limit_stop_prompt())
                note("context limit stop", f"{context} of {limit} tokens; round ended")
                return
            elif context >= limit * 0.9 and "90" not in fired:
                fired.update({"70", "90"})
                send(render_ladder_prioritized_prompt())
            elif context >= limit * 0.7 and "70" not in fired:
                fired.add("70")
                send(render_ladder_summary_prompt())

            for call in list(signals.calls):
                if (
                    call.denied
                    and call.command not in denial_ended
                    and signals.identical_denials(call.command) >= breaker.repeat_denial_cap
                ):
                    denial_ended.add(call.command)
                    end(render_repeat_denial_prompt(call.command))
                    note(
                        "repeat denial stop",
                        f"{breaker.repeat_denial_cap} denials of `{call.command}`; round ended",
                    )
                    return

            window = signals.stall_window()
            if window is not None:
                need = breaker.stall_window_k * (2 if signals.calls[-1].action == READ else 1)
                last = stall_nudged.get(window.start_call)
                if last is None or window.count >= last + need:
                    stall_nudged[window.start_call] = window.count
                    send(render_stall_nudge_prompt(window))
                    note("stall nudge", f"`{window.key[0]}` x{window.count}")

            for path, earlier in signals.redundant_reads().items():
                if path not in reminded:
                    reminded.add(path)
                    send(render_redundant_read_prompt(path, earlier))
                    note("redundant read reminder", f"{path} first read at turn {earlier}")

        on_turn.signals = signals  # type: ignore[attr-defined]
        return on_turn

    def _adopt_actual_session_id(self, result: RoundResult) -> None:
        """Reconcile the pre-registered coder id with the one the CLI really used.

        Plan U7 records the id *before* the fork, which assumes the fork uses it.
        A usage-limit retry breaks that assumption: the first attempt spends the
        id, so the retry mints a fresh one (see ``_call_with_retry``). Without
        this, the manifest keeps an id no session was ever created under, and a
        later resume would `--resume` into nothing — turning a recovered run
        into an unrecoverable one.
        """
        actual = result.session_id
        if not actual or actual == self.coder_sid:
            return
        self._log(
            f"group {self.gid} generation {self.generation}: coder session is {actual}, "
            f"not the pre-registered {self.coder_sid} (usage-limit retry minted a new id)"
        )
        self.coder_sid = actual
        self.coder_entry.session_id = actual

    def _refresh_transcript(self, entry: SessionEntry) -> None:
        """Fill in a pre-registered entry's transcript path once its session
        actually exists on disk (plan U7) — recorded before the fork call, so
        the path itself isn't known until the call returns.

        Authoritative over whatever `_watch_transcript` filled early (F9): a
        usage-limit retry can mint a fresh session id mid-fork, making the
        heartbeat's early path provisional — this overwrite, keyed on the
        adopted id, is what settles it."""
        self._heartbeat.on_tick = None
        entry.transcript_path = _transcript_str(self.deps.runner, entry.session_id)
        self.deps.store.save(self.deps.manifest)

    def _watch_transcript(self, entry: SessionEntry) -> None:
        """Fill ``transcript_path`` while the fork is still blocking (F9).

        `start_fork` blocks for the round's entire first turn — 8 to 24 minutes
        on r20260828-090936 — and until it returns the transcript endpoint 404s,
        so the operator cannot watch the very work they are waiting on. The
        transcript *file* appears as soon as the CLI starts writing; only the
        manifest pointer is missing. The group's heartbeat thread is the one
        thing awake during the fork, so it re-globs for the pre-registered id
        each tick and persists the path the moment the file exists. The hook
        self-clears once it has done its job; `_refresh_transcript` clears it
        unconditionally and overwrites the path after the fork returns."""

        def probe() -> None:
            if entry.transcript_path:
                self._heartbeat.on_tick = None
                return
            path = _transcript_str(self.deps.runner, entry.session_id)
            if path is not None:
                entry.transcript_path = path
                self.deps.store.save(self.deps.manifest)
                self._heartbeat.on_tick = None

        self._heartbeat.on_tick = probe

    def _persist_reviewer_usage(self, session_id: str) -> None:
        """Record the reviewer's latest context size on its manifest entry after
        every round (plan U7), mirroring ``_persist_coder_usage``: the reviewer
        entry otherwise carries a zero context-token count forever."""
        group_entry = self.deps.manifest.groups.get(self.gid)
        if group_entry is None:
            return
        for entry in reversed(group_entry.sessions):
            if entry.role == SessionRole.REVIEWER and entry.session_id == session_id:
                self._copy_usage(entry, session_id)
                self.deps.store.save(self.deps.manifest)
                return

    def _log_coder_session_end(self) -> None:
        """One line per coder session with what it cost — the run log used to
        carry no cost at all, and four of six run notes said so."""
        if self.coder_entry is None:
            return
        entry = self.coder_entry
        rounds = "round" if entry.rounds_completed == 1 else "rounds"
        self._log(
            f"group {self.gid} generation {self.generation}: coder session ended — "
            f"{entry.rounds_completed} {rounds}, ${entry.total_cost_usd:.2f}"
        )

    def _round_tag(self, round_no: int) -> str:
        # Side effect: records the round currently in flight (plan U10), so
        # `_on_content_filtered` can log the round a `ContentFiltered` ended
        # without a second, separate tracker to keep in sync with this one.
        self._current_round_no = round_no
        return f"group {self.gid} generation {self.generation} round {round_no}"

    def _record(self, role: SessionRole, session_id: str) -> SessionEntry:
        # started_at/model land here, at creation (plan U4) — not only once the
        # first round completes — so an in-flight group is distinguishable from
        # one that never started, and a crash before any round finishes still
        # leaves a manifest entry an operator can date.
        entry = SessionEntry(
            session_id=session_id,
            role=role,
            generation=self.generation,
            name=session_display_name(self.deps.run_id, self.gid, role.value, self.generation),
            transcript_path=_transcript_str(self.deps.runner, session_id),
            started_at=datetime.now(UTC).isoformat(),
            model=self.deps.runner.model,
        )
        record_session(
            self.deps.manifest,
            group_id=self.gid,
            group_name=self.group.name,
            summary=self.group.summary,
            entry=entry,
        )
        self.deps.store.save(self.deps.manifest)
        self.sessions_spawned += 1
        return entry


def _transcript_str(runner: SessionRunner, session_id: str) -> str | None:
    path = runner.transcript_path(session_id)
    return str(path) if path is not None else None


def _spec_hash(group: Group) -> str:
    """Identity of the spec a coder was launched on (see ``SessionEntry.spec_sha256``)."""
    return hashlib.sha256(group.model_dump_json().encode()).hexdigest()
