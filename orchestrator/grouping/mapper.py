"""Mapper: LLM plan-task extraction + codegraph-verified task→region mappings (R2).

The LLM proposes tasks and their code regions; every proposed symbol and file is
verified against the codegraph index and the working tree. Hallucinated regions
are dropped and flagged, never silently kept; tasks left with no regions ride
along as region-less nodes (the pipeline gives them a prose-affinity fallback).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from string import Template

from orchestrator.grouping.graphing import CodegraphClient, TaskMapping
from orchestrator.grouping.llm import JsonRunner, LlmCallRecorder, call_llm_json
from orchestrator.prompts import load_template

MAPPER_SCHEMA = {
    "title": "mapper_output",
    "type": "object",
    "required": ["tasks"],
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["task_id", "description", "files", "symbols"],
                "properties": {
                    "task_id": {"type": "string"},
                    "description": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}


@dataclass
class MapperOutput:
    """Verified mappings in plan order, task descriptions, and verification flags."""

    mappings: list[TaskMapping]
    descriptions: dict[str, str]
    flags: list[str] = field(default_factory=list)


def _validate_payload(payload: dict) -> list[dict]:
    tasks = payload["tasks"]
    if not isinstance(tasks, list):
        raise ValueError("'tasks' must be a list")
    for entry in tasks:
        if not isinstance(entry, dict):
            raise ValueError("each task entry must be an object")
        for key in ("task_id", "description"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise ValueError(f"task entry needs a non-empty string {key!r}")
        for key in ("files", "symbols"):
            value = entry.get(key)
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"task {entry['task_id']!r} {key!r} must be a list of strings")
    ids = [entry["task_id"] for entry in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task_id values")
    return tasks


def map_tasks(
    plan_text: str,
    runner: JsonRunner,
    client: CodegraphClient,
    max_retries: int = 2,
    failure_dir: Path | None = None,
    codegraph_files: str | None = None,
    recorder: LlmCallRecorder | None = None,
) -> MapperOutput:
    prompt = Template(load_template("mapper")).substitute(
        plan_text=plan_text,
        codegraph_files=codegraph_files if codegraph_files is not None else client.files_overview(),
    )
    tasks = call_llm_json(
        runner,
        prompt,
        MAPPER_SCHEMA,
        validate=_validate_payload,
        max_retries=max_retries,
        failure_dir=failure_dir,
        recorder=recorder,
    )

    mappings: list[TaskMapping] = []
    descriptions: dict[str, str] = {}
    flags: list[str] = []
    for entry in tasks:
        task_id = entry["task_id"]
        descriptions[task_id] = entry["description"]
        files = []
        for file in entry["files"]:
            if (client.repo_root / file).is_file():
                files.append(file)
            else:
                flags.append(f"mapper: task {task_id} mapped nonexistent file {file} — dropped")
        symbols = []
        for symbol in entry["symbols"]:
            if client.symbol_exists(symbol):
                symbols.append(symbol)
            else:
                flags.append(f"mapper: task {task_id} mapped unknown symbol {symbol} — dropped")
        if not files and not symbols:
            flags.append(
                f"mapper: task {task_id} has no verifiable regions — carried as "
                "region-less node with prose-affinity fallback"
            )
        mappings.append(TaskMapping(task_id, files=tuple(files), symbols=tuple(symbols)))
    return MapperOutput(mappings=mappings, descriptions=descriptions, flags=flags)
