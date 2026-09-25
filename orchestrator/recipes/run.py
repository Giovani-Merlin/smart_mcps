"""The ``run`` recipe: declared commands executed as confined Run Children."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestrator.config import EstimatorConfig
from orchestrator.model import VerificationResult
from orchestrator.recipes.registry import RecipePrice, UnitRecipe

SUMMARY_MAX_CHARS = 2000
# Fixed token allowance for the one-shot failure triage, and the wall clock a
# unit without a declared one takes (recorded in the grouping trace).
TRIAGE_TOKEN_ALLOWANCE = 20_000
DEFAULT_WALL_CLOCK_MIN = 10.0


class RunCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmd: str = Field(min_length=1)
    wall_clock_min: float = Field(gt=0)
    cwd: str | None = None


class RunArgs(BaseModel):
    """``recipe_args`` for a ``run`` unit — ``extra="forbid"`` so an unknown key
    is a hard parse error naming the field."""

    model_config = ConfigDict(extra="forbid")

    commands: list[RunCommand] = Field(min_length=1)
    outputs: list[str] = Field(default_factory=list)
    measurements: str | None = None
    commit_paths: list[str] = Field(default_factory=list)
    allow_write: list[str] = Field(default_factory=list)

    @field_validator("outputs")
    @classmethod
    def _outputs_repo_relative(cls, value: list[str]) -> list[str]:
        for entry in value:
            path = PurePosixPath(entry)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(
                    f"outputs entry {entry!r} must be repo-relative with no '..' component"
                )
        return value

    @field_validator("allow_write")
    @classmethod
    def _allow_write_safe(cls, value: list[str]) -> list[str]:
        claude_home = Path("~/.claude").expanduser()
        projects = claude_home / "projects"
        for entry in value:
            if "$" in entry:
                raise ValueError(f"allow_write entry {entry!r} must not use $VAR references")
            if not (entry.startswith("/") or entry == "~" or entry.startswith("~/")):
                raise ValueError(f"allow_write entry {entry!r} must be absolute or ~/-prefixed")
            expanded = Path(entry).expanduser()
            for protected in (claude_home, projects):
                # "equal to or containing": the entry either *is* the protected
                # dir, or is an ancestor of it (granting write there reopens it).
                if expanded == protected or expanded in protected.parents:
                    raise ValueError(
                        f"allow_write entry {entry!r} equals or contains {protected} — "
                        "operator memory stays unwritable"
                    )
        return value


class CommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmd: str
    exit_status: int
    duration_s: float
    output_paths: list[str] = Field(default_factory=list)


class RunRecord(BaseModel):
    """Completion contract of a ``run`` unit."""

    model_config = ConfigDict(extra="forbid")

    commands: list[CommandResult]
    outputs: list[str] = Field(default_factory=list)
    measurements: dict[str, object] = Field(default_factory=dict)
    summary: str = Field(max_length=SUMMARY_MAX_CHARS)


class RunVerificationReport(BaseModel):
    """The synthetic ``report-g<gen>-r<attempt>.json`` a completed ``run``
    group writes in place of a coder report: one ``pass`` per verification
    item whose ``Run:`` command is a declared command that exited 0. Read
    through ``latest_report`` by the report facts, the merge log and the
    driver-item auto-finish check, exactly like a coder's — so a run group is
    no longer "5/8 units landed … unverified" (r20260925-101742). Not a
    ``RunRecord`` field: that model is the Artifact Manifest contract folded
    into downstream prompts."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed"] = "completed"
    source: Literal["run_recipe"] = "run_recipe"
    summary: str
    verification_results: list[VerificationResult] = Field(default_factory=list)


#: ``Run: <cmd>`` / ``Run (…): `<cmd>` `` up to the ``Pass:`` clause or the end.
_RUN_ITEM_RE = re.compile(
    r"\bRun\s*(?:\([^)]*\))?\s*:\s*(?:`([^`]+)`|(.+?))(?=\s+Pass\s*:|$)",
    re.IGNORECASE | re.DOTALL,
)


def run_command_for_item(description: str, commands: Iterable[RunCommand]) -> RunCommand | None:
    """The declared command a verification item's ``Run:`` names, by
    whitespace-normalised equality with ``cmd.cmd``; ``None`` when the item
    has no ``Run:`` or names something else (a ``cd <cwd> && …`` rewrite is
    a non-goal — the plan writes the command as declared)."""
    match = _RUN_ITEM_RE.search(description)
    if match is None:
        return None
    wanted = " ".join((match.group(1) or match.group(2) or "").split())
    if not wanted:
        return None
    for command in commands:
        if " ".join(command.cmd.split()) == wanted:
            return command
    return None


def price_run(
    args: BaseModel | None, metadata: Mapping[str, object], config: EstimatorConfig
) -> RecipePrice:
    if isinstance(args, RunArgs) and args.commands:
        minutes = sum(c.wall_clock_min for c in args.commands)
        defaulted = False
    else:
        minutes, defaulted = DEFAULT_WALL_CLOCK_MIN, True
    return RecipePrice(
        tokens=float(metadata.get("triage_tokens") or TRIAGE_TOKEN_ALLOWANCE),
        wall_clock_s=minutes * 60.0,
        defaulted=defaulted,
    )


RUN_RECIPE = UnitRecipe(
    name="run",
    args_model=RunArgs,
    price=price_run,
    contract=RunRecord,
    reviewer_prompt=None,
    handoff_prompt=None,
    merge="run_commit_paths",
    executor="orchestrator.execution.run_executor:make_executor",
)
