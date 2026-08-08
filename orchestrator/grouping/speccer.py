"""Speccer: LLM group naming, analyzer-facing summaries, worker-facing specs (R6).

Runs after the deterministic core has decided boundaries — the speccer writes
prose *about* groups, it never moves tasks between them. Summaries are validated
against the analyzer's 120-char title cap and rejected, not truncated (plan U4).
"""

from __future__ import annotations

import json
from pathlib import Path
from string import Template

from pydantic import BaseModel, Field, ValidationError

from orchestrator.grouping.llm import JsonRunner, LlmCallRecorder, call_llm_json  # noqa: F401
from orchestrator.prompts import load_template
from orchestrator.model import SUMMARY_MAX_CHARS, VerificationItem

SPECCER_SCHEMA = {
    "title": "speccer_output",
    "type": "object",
    "required": ["groups"],
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["group_id", "name", "summary", "spec", "verification"],
                "properties": {
                    "group_id": {"type": "string"},
                    "name": {"type": "string"},
                    "summary": {"type": "string", "maxLength": SUMMARY_MAX_CHARS},
                    "spec": {"type": "string"},
                    "verification": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["id", "description"],
                            "properties": {
                                "id": {"type": "string"},
                                "description": {"type": "string"},
                                "required": {"type": "boolean"},
                            },
                        },
                    },
                },
            },
        }
    },
}


class GroupSpec(BaseModel):
    group_id: str
    name: str
    summary: str = Field(max_length=SUMMARY_MAX_CHARS)
    spec: str
    verification: list[VerificationItem] = Field(default_factory=list)


def write_specs(
    plan_text: str,
    skeletons: dict[str, dict],
    runner: JsonRunner,
    max_retries: int = 2,
    failure_dir: Path | None = None,
    recorder: LlmCallRecorder | None = None,
) -> dict[str, GroupSpec]:
    """Ask the LLM for names/summaries/specs/verification for every skeleton group.

    ``skeletons`` maps group_id → {tasks, descriptions, files} — the deterministic
    facts the prose must cover. Output must cover exactly the given group ids.
    """
    prompt = Template(load_template("speccer")).substitute(
        plan_text=plan_text,
        groups_json=json.dumps(skeletons, indent=2, sort_keys=True),
    )

    def validate(payload: dict) -> dict[str, GroupSpec]:
        entries = payload["groups"]
        if not isinstance(entries, list):
            raise ValueError("'groups' must be a list")
        try:
            specs = [GroupSpec.model_validate(entry) for entry in entries]
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc
        got = {spec.group_id for spec in specs}
        expected = set(skeletons)
        if got != expected:
            raise ValueError(f"group ids mismatch: expected {sorted(expected)}, got {sorted(got)}")
        return {spec.group_id: spec for spec in specs}

    return call_llm_json(
        runner,
        prompt,
        SPECCER_SCHEMA,
        validate=validate,
        max_retries=max_retries,
        failure_dir=failure_dir,
        recorder=recorder,
    )
