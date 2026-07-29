"""LLM call seam: `claude -p --json-schema` with bounded validation retries.

Every LLM touchpoint in the grouping engine goes through `call_llm_json` so tests
can stub the runner and never spend tokens (plan R24). Invalid output retries with
a corrective nudge, capped; persistent failure aborts with the raw output saved
for inspection (plan U4).
"""

from __future__ import annotations

import datetime
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

# (prompt, json_schema) → raw model text. The production runner shells the CLI;
# tests inject a canned function.
JsonRunner = Callable[[str, dict], str]

DEFAULT_MAX_RETRIES = 2


class LlmError(Exception):
    """The model never produced output that passed validation."""


class LlmProcessError(LlmError):
    """The ``claude -p`` process itself died — a non-zero exit, not bad output.

    Same envelope-failure class as ``SessionError`` on the session path: the CLI or
    API went away under the caller, so the work was never attempted and re-running
    it is free. Usage limits land here, with an empty stderr and exit 1. Kept
    distinct from its parent so the scheduler can classify it ``INTERRUPTED``
    (resumable) rather than ``FAILED`` (terminal) — a validation exhaustion is the
    model failing and stays terminal.
    """


def claude_json_runner(prompt: str, schema: dict) -> str:
    """Production runner: one blocking `claude -p` call with a JSON schema.

    Grouping-stage calls are stateless one-shots (no session to resume), so plain
    print mode is enough; the execution engine (U5) owns session lifecycles.
    """
    result = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "json", "--json-schema", json.dumps(schema)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise LlmProcessError(
            f"claude -p failed ({result.returncode}): {result.stderr.strip()[:500]}"
        )
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LlmError(f"claude -p emitted a non-JSON envelope: {exc}") from exc
    if not isinstance(envelope, dict) or "result" not in envelope:
        raise LlmError("claude -p envelope is missing the 'result' field")
    return str(envelope["result"])


def call_llm_json(
    runner: JsonRunner,
    prompt: str,
    schema: dict,
    validate: Callable[[dict], T],
    max_retries: int = DEFAULT_MAX_RETRIES,
    failure_dir: Path | None = None,
) -> T:
    """Run the LLM, parse JSON, validate; retry with a corrective nudge, capped."""
    stage = schema.get("title", "llm")
    attempt_prompt = prompt
    last_raw = ""
    last_error: Exception | None = None
    for _attempt in range(1 + max_retries):
        last_raw = runner(attempt_prompt, schema)
        try:
            payload = json.loads(last_raw)
            return validate(payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            last_error = exc
            attempt_prompt = (
                f"{prompt}\n\nYour previous output failed validation: {exc}.\n"
                "Return ONLY valid JSON matching the schema — no prose, no fences."
            )
    saved = _save_failure(failure_dir, stage, last_raw)
    location = f"; raw output saved to {saved}" if saved else ""
    raise LlmError(
        f"{stage} output failed validation after {1 + max_retries} attempts: {last_error}{location}"
    )


def _save_failure(failure_dir: Path | None, stage: str, raw: str) -> Path | None:
    if failure_dir is None:
        return None
    failure_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S")
    path = failure_dir / f"{stage}-{stamp}.txt"
    path.write_text(raw)
    return path
