"""The ``evaluate`` recipe's executor (plan U8): the ``run`` recipe's declared
commands, plus a hash-checked harness and an optional smoke gate before them,
and a KPI/guard extraction after measurements are read.

The harness hash baseline lives at ``<group_dir>/eval/harness.sha256`` — a
JSON snapshot (``{"combined": ..., "paths": {path: sha256}}``) written on the
first evaluation and compared on every later one; a mismatch names the first
differing path and fails the attempt without spending a triage call (the
harness itself is untrustworthy, so there is nothing for triage to diagnose).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from orchestrator.execution.command_runner import CommandDied, TimedOut
from orchestrator.execution.manifest import atomic_write_text, log_event
from orchestrator.execution.run_executor import _RunExecution
from orchestrator.execution.scheduler import Executor, GroupContext, GroupFailure, GroupState
from orchestrator.recipes.evaluate import EvaluateArgs, EvaluationRecord
from orchestrator.recipes.run import CommandResult

SMOKE_WALL_CLOCK_MIN = 5.0
#: the launch wrapper writes ``<n>.state.json`` / ``.out`` / ``.err`` /
#: ``.exit`` — 0 keeps the smoke command's files distinct from every
#: declared command's (1-indexed).
SMOKE_COMMAND_INDEX = 0


def make_executor(deps) -> Executor:
    async def executor(ctx: GroupContext) -> GroupState:
        return await EvaluateExecution(deps, ctx).run()

    return executor


def _hash_file(path: Path) -> str:
    if not path.is_file():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _harness_snapshot(workspace: Path, harness_paths: list[str]) -> dict[str, str]:
    return {p: _hash_file(workspace / p) for p in harness_paths}


def _combined_hash(snapshot: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(snapshot):
        digest.update(path.encode())
        digest.update(snapshot[path].encode())
    return digest.hexdigest()


class EvaluateExecution(_RunExecution):
    _args_model = EvaluateArgs

    def __init__(self, deps, ctx: GroupContext):
        super().__init__(deps, ctx)
        self.args: EvaluateArgs = self.args
        self._harness_hash = ""
        self._kpi_value: float | None = None
        self._guard_values: dict[str, float] = {}
        self._threshold_cleared: bool | None = None

    def _eval_dir(self) -> Path:
        return self._run_dir() / "eval"

    async def _before_commands(self, attempt_dir: Path) -> None:
        kpi = self.args.kpi
        snapshot = _harness_snapshot(self.workspace, kpi.harness_paths)
        combined = _combined_hash(snapshot)
        baseline_path = self._eval_dir() / "harness.sha256"
        if baseline_path.is_file():
            try:
                baseline = json.loads(baseline_path.read_text())
            except (OSError, ValueError):
                baseline = {}
            baseline_paths = baseline.get("paths", {})
            first_diff = next(
                (p for p in kpi.harness_paths if baseline_paths.get(p) != snapshot.get(p)), None
            )
            if first_diff is not None:
                message = (
                    f"group {self.gid}: harness path changed since the first evaluation: "
                    f"{first_diff}"
                )
                log_event(self.paths, f"group {self.gid}: evaluate failure — {message}")
                self._commands.write_settled(attempt_dir, "failed", message)
                raise GroupFailure(message)
        else:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                baseline_path,
                json.dumps({"combined": combined, "paths": snapshot}, indent=2) + "\n",
            )
        self._harness_hash = combined
        await self._run_smoke(attempt_dir)

    async def _run_smoke(self, attempt_dir: Path) -> None:
        smoke = self.args.kpi.smoke
        if not smoke:
            return
        command = SimpleNamespace(cmd=smoke, wall_clock_min=SMOKE_WALL_CLOCK_MIN)
        preexec_fn = self._confinement_preexec(self.workspace, attempt_dir)
        try:
            result = await self._commands.run(
                attempt_dir,
                SMOKE_COMMAND_INDEX,
                command,
                self.workspace,
                preexec_fn=preexec_fn,
                env=self._child_env(),
                total=len(self.args.commands) + 1,
            )
        except (TimedOut, CommandDied) as exc:
            await self._triage_and_fail(f"group {self.gid}: smoke failed: {exc}")
            return
        if result.exit_status != 0:
            await self._triage_and_fail(
                f"group {self.gid}: smoke failed (exit {result.exit_status})"
            )

    async def _after_measurements(self, measurements: dict, measurements_missing: bool) -> None:
        kpi = self.args.kpi
        value = measurements.get(kpi.key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            await self._triage_and_fail(
                f"group {self.gid}: measurements missing KPI key {kpi.key!r}"
            )
            return
        self._kpi_value = float(value)
        guard_values: dict[str, float] = {}
        for guard in kpi.guards:
            guard_value = measurements.get(guard.key)
            if not isinstance(guard_value, (int, float)) or isinstance(guard_value, bool):
                await self._triage_and_fail(
                    f"group {self.gid}: measurements missing guard key {guard.key!r}"
                )
                return
            guard_values[guard.key] = float(guard_value)
        self._guard_values = guard_values
        if kpi.good_enough is not None:
            if kpi.direction == "max":
                self._threshold_cleared = self._kpi_value >= kpi.good_enough
            else:
                self._threshold_cleared = self._kpi_value <= kpi.good_enough

    def _build_record(
        self, results: list[CommandResult], measurements: dict, summary: str
    ) -> EvaluationRecord:
        return EvaluationRecord(
            commands=results,
            outputs=list(self.args.outputs),
            measurements=measurements,
            summary=summary,
            kpi_key=self.args.kpi.key,
            kpi_value=self._kpi_value,
            guards=self._guard_values,
            harness_hash=self._harness_hash,
            threshold_cleared=self._threshold_cleared,
        )
