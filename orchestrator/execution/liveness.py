"""In-process activity registry for stream-json worker children (plan U1).

This is the foundation the Sign of Life mechanism (plan U2) stands on: every
stream event a worker child emits stamps an ``ActivityRegistry`` entry, so a
later per-tick probe can ask "what did this group's child last do, and when"
without parsing a transcript or touching disk. Nothing here decides whether a
child is stuck — R3 keeps that call, evidence-only, one layer up in the
liveness probe. This module only ever records facts: it has no window, no
threshold, and no verdict.

The registry lives for the lifetime of the orchestrator process. It is fed by
``SessionRunner._spawn`` and read by ``RoundHeartbeat`` (via an
``activity_provider`` callable) and by the liveness probe. Every method is
thread-safe and none of them can raise on a caller's behalf — a session
lookup or a hook failure must never take down a round, the same contract
``heartbeat.py`` already holds itself to.
"""

from __future__ import annotations

import datetime
import json
import os
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator.execution.manifest import RunPaths, atomic_write_text

if TYPE_CHECKING:
    from orchestrator.config import LivenessConfig
    from orchestrator.execution.heartbeat import RoundHeartbeat

#: How much of the last assistant turn's text is worth keeping. This rides on
#: an in-memory record, not a report, so it is capped generously but still
#: bounded — a worker cannot make this grow without limit just by talking.
_LAST_ASSISTANT_TEXT_MAX_CHARS = 2000


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds")


def _humanize_age(seconds: float) -> str:
    """Same rendering as ``heartbeat._humanize``, duplicated rather than
    imported: ``heartbeat.py`` already imports ``ChildActivity`` from this
    module, and importing back would make the two circular."""
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


@dataclass
class ChildActivity:
    """Everything the registry knows about one live (or just-exited) worker
    child. ``cured`` is set once by the liveness probe (plan U5) after it has
    killed this pid as a Suspend Cure, so the runner can tell that apart from
    a genuine failure."""

    pid: int
    session_id: str
    cwd: str
    spawned_at: str
    last_event_at: str | None = None
    last_event_type: str | None = None
    last_assistant_text: str = ""
    cured: bool = False


class ActivityRegistry:
    """Thread-safe, in-process record of every worker child's most recent
    activity, keyed by pid.

    Entries are never evicted on ``exited`` — a pid that has just exited is
    exactly the one ``was_cured`` needs to answer for, in ``SessionRunner``'s
    own next few lines of code — so ``exited`` only drops the pid from the
    "live" set that ``current`` searches. The dict itself lives for the
    process's lifetime; a pid is a small, bounded record, and a run's worker
    count is nowhere near large enough for that to matter.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._children: dict[int, ChildActivity] = {}
        # Spawn order, oldest first — `current` walks it in reverse so the
        # newest spawn for a given cwd wins, per the contract.
        self._order: list[int] = []
        self._live: set[int] = set()

    def spawned(self, pid: int, *, session_id: str, cwd: str) -> None:
        with self._lock:
            self._children[pid] = ChildActivity(
                pid=pid, session_id=session_id, cwd=cwd, spawned_at=_now()
            )
            self._order.append(pid)
            self._live.add(pid)

    def note_event(self, pid: int, event_type: str) -> None:
        """Stamp the latest stream event a child produced. A no-op for a pid
        the registry never saw ``spawned`` for — never raises, since the
        stream reader that calls this must never be interrupted by it."""
        with self._lock:
            child = self._children.get(pid)
            if child is None:
                return
            child.last_event_at = _now()
            child.last_event_type = event_type

    def note_assistant_text(self, pid: int, text: str) -> None:
        """Record the text of the child's last assistant turn. A no-op for an
        unknown pid, same contract as ``note_event``."""
        with self._lock:
            child = self._children.get(pid)
            if child is None:
                return
            child.last_assistant_text = text[:_LAST_ASSISTANT_TEXT_MAX_CHARS]

    def exited(self, pid: int) -> None:
        with self._lock:
            self._live.discard(pid)

    def current(self, cwd: str) -> ChildActivity | None:
        """The newest live child whose cwd equals *cwd*, or ``None`` when
        none is live there (including once its exit has been recorded)."""
        with self._lock:
            for pid in reversed(self._order):
                if pid not in self._live:
                    continue
                child = self._children.get(pid)
                if child is not None and child.cwd == cwd:
                    return child
        return None

    def mark_cured(self, pid: int) -> None:
        with self._lock:
            child = self._children.get(pid)
            if child is not None:
                child.cured = True

    def was_cured(self, pid: int) -> bool:
        with self._lock:
            child = self._children.get(pid)
            return bool(child is not None and child.cured)


# --------------------------------------------------------------- /proc reads
#
# Plan U2: pure helpers over a swappable ``proc_root`` so tests can point them
# at a fake tree under ``tmp_path`` instead of the real ``/proc``. A read that
# races a process's exit (the directory vanishes between listing and opening
# a file inside it, or the file is gone by the time it is read) is exactly as
# likely on the real kernel as it is contrived in a fixture, so every helper
# here treats ENOENT/OSError as "nothing to report", never an exception.


@dataclass
class ProcChild:
    """One live, non-zombie process found under ``/proc`` whose parent is the
    pid being probed."""

    pid: int
    cmdline: str


def _read_stat_fields(pid: int, proc_root: Path) -> list[str] | None:
    """The space-split remainder of ``/proc/<pid>/stat`` after its
    parenthesised ``comm`` field, which itself may contain spaces (and even
    unbalanced parens) — splitting on the *last* ``)`` is the only reliable
    way in, and is what the kernel's own documentation recommends."""
    try:
        text = (proc_root / str(pid) / "stat").read_text()
    except OSError:
        return None
    try:
        idx = text.rindex(")")
    except ValueError:
        return None
    return text[idx + 1 :].split()


def _read_cmdline(pid: int, proc_root: Path) -> str:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    parts = [part.decode(errors="replace") for part in raw.split(b"\x00") if part]
    return " ".join(parts)


def cpu_ticks(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """utime + stime (fields 14 and 15 of ``stat``), or ``None`` when the pid
    cannot be read at all — a process that has since exited, most commonly."""
    fields = _read_stat_fields(pid, proc_root)
    if fields is None or len(fields) < 13:
        return None
    try:
        return int(fields[11]) + int(fields[12])
    except ValueError:
        return None


def children_of(pid: int, *, proc_root: Path = Path("/proc")) -> list[ProcChild]:
    """Live, non-zombie processes whose ``ppid`` is *pid*. A pid whose stat
    file cannot be read (raced exit) is silently skipped, not raised on."""
    out: list[ProcChild] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.name.isdigit():
            continue
        child_pid = int(entry.name)
        fields = _read_stat_fields(child_pid, proc_root)
        if fields is None or len(fields) < 2:
            continue
        state = fields[0]
        try:
            ppid = int(fields[1])
        except ValueError:
            continue
        if ppid != pid or state == "Z":
            continue
        out.append(ProcChild(pid=child_pid, cmdline=_read_cmdline(child_pid, proc_root)))
    return out


def descendants(pid: int, *, proc_root: Path = Path("/proc")) -> list[ProcChild]:
    """Every live, non-zombie descendant of *pid*, breadth-first. Used by the
    Suspend Cure (plan U5) to kill a child's whole pid tree; U2 ships it
    alongside ``children_of`` because both walk the same ``/proc`` listing."""
    result: list[ProcChild] = []
    seen = {pid}
    frontier = [pid]
    while frontier:
        next_frontier: list[int] = []
        for parent in frontier:
            for child in children_of(parent, proc_root=proc_root):
                if child.pid in seen:
                    continue
                seen.add(child.pid)
                result.append(child)
                next_frontier.append(child.pid)
        frontier = next_frontier
    return result


def _proc_alive(pid: int, proc_root: Path) -> bool:
    """Whether *pid* still has a readable ``/proc/<pid>/stat`` — the same
    liveness test the rest of this module already uses, so a pid this
    function calls dead is dead by the identical definition every other
    reader here applies."""
    return _read_stat_fields(pid, proc_root) is not None


def kill_tree(
    pid: int,
    *,
    grace_s: float,
    proc_root: Path = Path("/proc"),
    kill: Callable[[int, int], None] = os.kill,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> list[int]:
    """The Suspend Cure's kill (plan U5): SIGTERM *pid* and every live
    descendant found under *proc_root*, wait up to *grace_s* for each to
    disappear, then SIGKILL whatever is left. Returns every pid the SIGTERM
    reached (not just the ones that needed the SIGKILL follow-up) — the
    caller's evidence of what it actually signalled.

    A pid that has already exited before its SIGTERM — the child died on its
    own between the probe's read and this call, or a descendant snapshot that
    raced an exit — is skipped without raising, same contract every ``/proc``
    reader in this module holds. Descendants are read once, up front: a
    process tree that changes shape mid-kill is not re-walked, since the goal
    is "the tree this probe observed," not a moving target.
    """
    targets = [pid, *(child.pid for child in descendants(pid, proc_root=proc_root))]
    signalled: list[int] = []
    for target in targets:
        try:
            kill(target, signal.SIGTERM)
        except ProcessLookupError:
            continue
        signalled.append(target)

    deadline = clock() + grace_s
    remaining = set(signalled)
    while remaining and clock() < deadline:
        remaining = {t for t in remaining if _proc_alive(t, proc_root)}
        if remaining:
            sleep(0.05)
    for target in remaining:
        if _proc_alive(target, proc_root):
            try:
                kill(target, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return signalled


# ------------------------------------------------------------ sign of life


@dataclass
class SignOfLife:
    """One tick's evaluation of the three signals against a group's child.

    ``at`` is the timestamp the winning signal fired at (``None`` when no
    signal fired this tick — the caller decides whether to keep the previous
    ``last_sign_of_life_at`` or not, this module never remembers state across
    calls). ``cpu_ticks`` is always returned when readable, signal or not, so
    the caller can feed it back in as next tick's ``prev_cpu`` even on a tick
    that reported no signal.
    """

    at: str | None
    signal: str | None  # "event" | "tool_child" | "cpu" | None
    evidence: str
    cpu_ticks: int | None


def sign_of_life(
    child: ChildActivity,
    *,
    prev_cpu: int | None,
    now: float,
    window_s: float,
    proc_root: Path = Path("/proc"),
) -> SignOfLife:
    """Evaluate the three signals, in priority order, against one child:

    (a) a stream event within the Liveness Window,
    (b) a live, non-zombie process whose parent is the child, or
    (c) CPU ticks having advanced since the last sample.

    Facts only — this never decides "stuck", it decides whether *this tick*
    saw evidence of life and, if so, what kind.
    """
    current_cpu = cpu_ticks(child.pid, proc_root=proc_root)
    at = datetime.datetime.fromtimestamp(now, tz=datetime.UTC).isoformat(timespec="milliseconds")

    event_at = _parse_ts(child.last_event_at)
    if event_at is not None and (now - event_at) <= window_s:
        evidence = f"event {child.last_event_type} {_humanize_age(now - event_at)} ago"
        return SignOfLife(at=at, signal="event", evidence=evidence, cpu_ticks=current_cpu)

    kids = children_of(child.pid, proc_root=proc_root)
    if kids:
        head = kids[0].cmdline or f"pid {kids[0].pid}"
        evidence = f'tool child "{head}" running'
        return SignOfLife(at=at, signal="tool_child", evidence=evidence, cpu_ticks=current_cpu)

    if current_cpu is not None and prev_cpu is not None and current_cpu > prev_cpu:
        return SignOfLife(at=at, signal="cpu", evidence="cpu advancing", cpu_ticks=current_cpu)

    # A readable-but-unmoving cpu reading (a suspended real process, most
    # commonly SIGSTOP) is a distinct piece of evidence from a pid this probe
    # cannot read at all — the latter says nothing about whether the process
    # is stuck, the former is direct proof that it is.
    evidence = "cpu flat" if current_cpu is not None else "no signal"
    return SignOfLife(at=None, signal=None, evidence=evidence, cpu_ticks=current_cpu)


# ------------------------------------------------------------ liveness probe

#: Phases whose meaning is "nothing has happened yet" rather than "the round
#: is under way" — flipped to a `round N running`-class phase the moment the
#: first assistant event lands, because the CLI's `system` init event arrives
#: within a second and would otherwise make the launch phase meaningless for
#: the whole round.
LAUNCH_PHASES: dict[str, str] = {
    "starting the coder": "running",
    "forking the base session": "running",
    "resuming the interrupted coder": "running",
    "reviewer verifying the report": "review running",
}


def should_cure(
    *,
    last_wake_at: str | None,
    last_sign_of_life_at: str | None,
    child_spawned_at: str,
    now: float,
    window_s: float,
) -> bool:
    """The Suspend Cure eligibility predicate (plan U5) — everything except
    the per-generation cap, which the caller checks separately so it can
    still log "cures exhausted" for a child that is otherwise eligible.

    Pure and side-effect-free so the table of cases tests directly: no cure
    without a recorded machine wake (R7's last sentence — Not Live alone is
    never enough, no matter how long); no cure when a Sign of Life has been
    seen since that wake; no cure until a whole Liveness Window has passed
    since the wake.
    """
    if last_wake_at is None:
        return False
    wake = _parse_ts(last_wake_at)
    if wake is None:
        return False
    candidates = [_parse_ts(last_sign_of_life_at), _parse_ts(child_spawned_at)]
    baselines = [b for b in candidates if b is not None]
    baseline = max(baselines) if baselines else None
    if baseline is not None and baseline >= wake:
        return False
    return now - wake > window_s


class LivenessProbe:
    """Evaluates Sign of Life once per heartbeat tick and writes the facts
    into that heartbeat (plan U2/U3), and — when wired with the Suspend Cure
    seams — cures a child that has shown no Sign of Life since the machine's
    last recorded wake (plan U5).

    Install with ``heartbeat.add_tick_hook(probe.tick)`` — never assign to
    ``heartbeat.on_tick``: that slot belongs to the review loop's transcript
    probe (F9), which is toggled on and off through a round, while this probe
    must keep running for the group's entire life.
    """

    def __init__(
        self,
        heartbeat: RoundHeartbeat,
        config: LivenessConfig,
        activity_provider: Callable[[], ChildActivity | None],
        log: Callable[[str], None] | None = None,
        *,
        proc_root: Path = Path("/proc"),
        clock: Callable[[], float] = time.time,
        suspend_facts_provider: Callable[[], SuspendFacts | None] | None = None,
        cures_provider: Callable[[], int] | None = None,
        on_cure: Callable[[int, int, str], None] | None = None,
        activity: ActivityRegistry | None = None,
        kill: Callable[[int, int], None] = os.kill,
    ) -> None:
        self.heartbeat = heartbeat
        self.config = config
        self.activity_provider = activity_provider
        self._log = log
        self.proc_root = proc_root
        self._clock = clock
        self._prev_cpu: dict[int, int] = {}
        self._not_live_since: float | None = None
        # plan U5: all four are optional and default to "cures never happen" —
        # a probe built without them (every U2/U3 test, and any caller that
        # predates this unit) behaves exactly as before.
        self.suspend_facts_provider = suspend_facts_provider
        self.cures_provider = cures_provider
        self.on_cure = on_cure
        self.activity = activity
        self._kill = kill
        self._cures_exhausted_logged = False

    def tick(self) -> None:
        now = self._clock()
        child = self._current_activity()
        if child is None:
            self._clear_not_live_on_exit()
            return

        prev = self._prev_cpu.get(child.pid)
        result = sign_of_life(
            child,
            prev_cpu=prev,
            now=now,
            window_s=self.config.window_seconds,
            proc_root=self.proc_root,
        )
        if result.cpu_ticks is not None:
            self._prev_cpu[child.pid] = result.cpu_ticks

        # `last_sign_of_life_at` is the one fact that outlives a quiet tick —
        # it is the baseline every reader ages against, and a tick with no
        # signal must not reset it to "now". `sign_of_life_signal` and
        # `sign_of_life_evidence` always reflect *this* tick's own evaluation,
        # signal or not, since they describe what was just observed.
        if result.signal is None:
            last_sign_of_life_at = self.heartbeat.liveness_facts().get("last_sign_of_life_at")
        else:
            last_sign_of_life_at = result.at

        cures = self.cures_provider() if self.cures_provider is not None else 0
        facts = {
            "last_sign_of_life_at": last_sign_of_life_at,
            "sign_of_life_signal": result.signal,
            "sign_of_life_evidence": result.evidence,
            "liveness_window_s": self.config.window_seconds,
            "cures": cures,
            "max_cures_per_generation": self.config.max_cures_per_generation,
        }
        self.heartbeat.set_liveness_facts(facts)

        self._check_phase_flip(child)
        self._check_transition(facts, now, child)
        self._check_cure(facts, now, child, cures)

    def _current_activity(self) -> ChildActivity | None:
        try:
            return self.activity_provider()
        except Exception:  # noqa: BLE001 - evidence is never worth a round
            return None

    def _check_phase_flip(self, child: ChildActivity) -> None:
        if child.last_event_type != "assistant":
            return
        phase = self.heartbeat.current_phase()
        if phase is None:
            return
        suffix = LAUNCH_PHASES.get(phase)
        if suffix is None:
            return
        self.heartbeat.mark_phase(f"round {self.heartbeat.round_no} {suffix}")

    def _check_transition(self, facts: dict, now: float, child: ChildActivity) -> None:
        window = facts["liveness_window_s"]
        baseline_raw = facts["last_sign_of_life_at"] or child.spawned_at
        baseline = _parse_ts(baseline_raw)
        age = now - baseline if baseline is not None else 0.0
        gid = self.heartbeat.group_id
        generation = self.heartbeat.generation_no
        phase = self.heartbeat.current_phase() or "unknown phase"

        if age > window and self._not_live_since is None:
            self._not_live_since = now
            self._log_line(
                f"group {gid} generation {generation}: not live for "
                f"{_humanize_age(age)} in {phase} — {facts['sign_of_life_evidence']}"
            )
        elif age <= window and self._not_live_since is not None:
            self._not_live_since = None
            self._log_line(
                f"group {gid} generation {generation}: live again: "
                f"{facts['sign_of_life_signal']} {_humanize_age(age)} ago"
            )

    def _clear_not_live_on_exit(self) -> None:
        if self._not_live_since is None:
            return
        self._not_live_since = None
        gid = self.heartbeat.group_id
        generation = self.heartbeat.generation_no
        self._log_line(f"group {gid} generation {generation}: live again: child exited")

    def _check_cure(self, facts: dict, now: float, child: ChildActivity, cures: int) -> None:
        """Plan U5: kill and warm-resume-flag a child with no Sign of Life
        since the machine's last recorded wake. A no-op when the seams were
        never wired (``suspend_facts_provider``/``cures_provider`` unset) —
        the U2/U3 launch phases and Not Live reporting run identically either
        way."""
        if self.suspend_facts_provider is None or self.cures_provider is None:
            return
        wake_facts = self.suspend_facts_provider()
        if wake_facts is None or wake_facts.last_wake_at is None:
            return
        if not should_cure(
            last_wake_at=wake_facts.last_wake_at,
            last_sign_of_life_at=facts["last_sign_of_life_at"],
            child_spawned_at=child.spawned_at,
            now=now,
            window_s=facts["liveness_window_s"],
        ):
            return
        gid = self.heartbeat.group_id
        generation = self.heartbeat.generation_no
        max_cures = self.config.max_cures_per_generation
        if cures >= max_cures:
            if not self._cures_exhausted_logged:
                self._cures_exhausted_logged = True
                self._log_line(
                    f"group {gid} generation {generation}: cures exhausted "
                    f"({cures}/{max_cures}); reporting only"
                )
            return
        self._log_line(
            f"group {gid} generation {generation}: suspend cure {cures + 1}/{max_cures} — "
            f"no sign of life since wake at {wake_facts.last_wake_at}; "
            f"killing session {child.session_id} pid {child.pid}"
        )
        if self.activity is not None:
            self.activity.mark_cured(child.pid)
        kill_tree(
            child.pid,
            grace_s=self.config.kill_grace_seconds,
            proc_root=self.proc_root,
            kill=self._kill,
        )
        if self.on_cure is not None:
            self.on_cure(child.pid, generation, child.session_id)

    def _log_line(self, line: str) -> None:
        if self._log is None:
            return
        try:
            self._log(line)
        except Exception:  # noqa: BLE001 - evidence is never worth a round
            pass


# --------------------------------------------------------- reader-side facts


def not_live_age(heartbeat: dict, *, now: float) -> float | None:
    """Seconds past the Liveness Window since *heartbeat*'s child last showed
    a Sign of Life, or ``None`` when the child is live, there is no child, or
    the file predates these facts (``liveness_window_s`` absent).

    The single derivation rule every reader — ``liveness_line``, the driver's
    group count, the Observatory's client-side render — applies identically,
    so "Not Live" is never persisted anywhere, only ever computed from facts.
    """
    if heartbeat.get("child_pid") is None:
        return None
    window = heartbeat.get("liveness_window_s")
    if window is None:
        return None
    candidates = [
        _parse_ts(heartbeat.get("last_sign_of_life_at")),
        _parse_ts(heartbeat.get("child_spawned_at")),
    ]
    baselines = [c for c in candidates if c is not None]
    if not baselines:
        return None
    age = now - max(baselines)
    return age if age > window else None


def liveness_line(heartbeat: dict, *, now: float) -> str:
    """One human-readable line for `status` and the Observatory, derived
    entirely from a heartbeat's own facts — see ``not_live_age``."""
    if heartbeat.get("liveness_window_s") is None:
        return "(no liveness facts)"
    phase = heartbeat.get("phase") or "unknown phase"
    if heartbeat.get("child_pid") is None:
        return f"no worker child ({phase})"
    age = not_live_age(heartbeat, now=now)
    if age is not None:
        evidence = heartbeat.get("sign_of_life_evidence") or "no evidence recorded"
        return f"NOT LIVE for {_humanize_age(age)} in {phase} — {evidence}"
    signal = heartbeat.get("sign_of_life_signal")
    last = _parse_ts(heartbeat.get("last_sign_of_life_at"))
    live_age = now - last if last is not None else 0.0
    return f"live: {signal} {_humanize_age(live_age)} ago"


def cures_exhausted_line(heartbeat: dict, run_id: str, driver_pid: int) -> str | None:
    """The manual-intervention line once a generation has used up its Suspend
    Cures, or ``None`` below the cap. ``<pgid>`` is read from the *driver's*
    pid, not the child's — this is the command an operator runs against the
    process group Ctrl-C would already reach."""
    cures = heartbeat.get("cures")
    max_cures = heartbeat.get("max_cures_per_generation")
    if cures is None or max_cures is None or cures < max_cures:
        return None
    pgid = os.getpgid(driver_pid)
    return (
        f"cures exhausted ({cures}/{max_cures} this generation) — "
        f"kill -INT -{pgid} then smart-mcps-orchestrate resume {run_id}"
    )


# ---------------------------------------------------------- suspend detection

#: Schema for the suspend facts merged into the run-scoped heartbeat file.
#: Independent of ``heartbeat.SCHEMA_VERSION`` — this module never imports
#: ``heartbeat.py`` (that module imports this one, for ``ChildActivity``), so
#: bumping one on its own never forces a bump of the other.
SUSPEND_SCHEMA_VERSION = 1


def _run_heartbeat_path(paths: RunPaths) -> Path:
    """Same file ``heartbeat.heartbeat_path(paths, group_id=None)`` names,
    duplicated rather than imported: ``heartbeat.py`` already imports
    ``ChildActivity`` from this module, and importing back would make the two
    circular. Post-ADR-0007 runs never write a run-scoped heartbeat for phase
    facts, which is exactly what leaves this file free for suspend facts."""
    return paths.run_dir / "heartbeat.json"


@dataclass
class SuspendFacts:
    """What ``<run>/heartbeat.json`` says about machine suspends so far."""

    last_wake_at: str | None
    last_suspend_gap_s: float | None
    suspends: int


def read_suspend_facts(paths: RunPaths) -> SuspendFacts | None:
    """The suspend facts on disk, or ``None`` when none have ever been
    written — tolerant of a missing, malformed, or pre-suspend-facts file,
    same contract as ``heartbeat.read_heartbeat``."""
    try:
        payload = json.loads(_run_heartbeat_path(paths).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or "last_wake_at" not in payload:
        return None
    return SuspendFacts(
        last_wake_at=payload.get("last_wake_at"),
        last_suspend_gap_s=payload.get("last_suspend_gap_s"),
        suspends=payload.get("suspends", 0),
    )


class SuspendMonitor:
    """Detects a machine suspend by comparing ``CLOCK_MONOTONIC`` (frozen
    while suspended) against ``CLOCK_BOOTTIME`` (keeps advancing) between two
    samples, with a wall-clock fallback for a platform where the two clocks
    do not diverge across a suspend (WSL2).

    ``sample()`` is pure and side-effect-free except for the detection path:
    it always advances the monitor's own baseline, and only when it detects a
    gap does it write the run-scoped heartbeat file and log a line. The first
    call after construction only establishes the baseline — there is nothing
    to compare against yet — so it never reports a suspend.
    """

    def __init__(
        self,
        paths: RunPaths,
        *,
        gap_s: float,
        interval_s: float,
        log: Callable[[str], None],
        clock: Callable[[], float] = time.monotonic,
        boottime: Callable[[], float] = lambda: time.clock_gettime(time.CLOCK_BOOTTIME),
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.paths = paths
        self.gap_s = gap_s
        self.interval_s = interval_s
        self._log = log
        self._clock = clock
        self._boottime = boottime
        self._wall = wall
        self._prev_mono: float | None = None
        self._prev_boot: float | None = None
        self._prev_wall: float | None = None

    def sample(self) -> float | None:
        mono = self._clock()
        boot = self._boottime()
        wall = self._wall()
        gap: float | None = None
        if self._prev_mono is not None:
            mono_delta = mono - self._prev_mono
            boot_delta = boot - self._prev_boot
            wall_delta = wall - self._prev_wall
            boot_gap = boot_delta - mono_delta
            if boot_gap > self.gap_s:
                gap = boot_gap
            elif wall_delta > 5 * self.interval_s:
                gap = wall_delta
        self._prev_mono = mono
        self._prev_boot = boot
        self._prev_wall = wall
        if gap is not None:
            self._record(gap, wall)
        return gap

    def _record(self, gap: float, wall_now: float) -> None:
        """Best-effort, like the heartbeat: an unwritable run directory loses
        the evidence, never the run."""
        try:
            path = _run_heartbeat_path(self.paths)
            try:
                existing = json.loads(path.read_text())
                if not isinstance(existing, dict):
                    existing = {}
            except (OSError, json.JSONDecodeError):
                existing = {}
            now_iso = datetime.datetime.fromtimestamp(wall_now, tz=datetime.UTC).isoformat(
                timespec="seconds"
            )
            existing.update(
                {
                    "schema_version": SUSPEND_SCHEMA_VERSION,
                    "last_wake_at": now_iso,
                    "last_suspend_gap_s": gap,
                    "suspends": existing.get("suspends", 0) + 1,
                    "updated_at": now_iso,
                }
            )
            atomic_write_text(path, json.dumps(existing, indent=2) + "\n")
        except Exception:  # noqa: BLE001 - evidence is never worth the run
            pass
        self._log_line(f"machine suspend detected: {_humanize_age(gap)}")

    def _log_line(self, line: str) -> None:
        try:
            self._log(line)
        except Exception:  # noqa: BLE001 - evidence is never worth the run
            pass
