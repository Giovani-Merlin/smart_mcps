"""Repo-aware lint of a plan's ``Run:`` verification items (``plan-check``).

Two plan defects cost run r20260927-100604 a halted run and a driver
correction, and both were visible in the plan text before launch:

- **g2-7** was a coder item ``bash -c 'echo 5 > /tmp/v && …'``: ``bash`` is not
  on the worker allowlist, so the permission layer refused it three times
  (``harness_allowlist``) and the group was interrupted. (``/tmp`` itself is
  writable under confinement; the program was the problem.)
- **g3-2** named ``.orchestrator/runs/<id>/ingest.json``, a path no code ever
  writes (export writes ``ingest/ingest.json``).

So, for every ``Run:`` command that is not already a ``Run (driver):`` item, a
segment whose program the orchestrator's default allowlist does not grant is a
**warning** — the operator's own settings may add it, which this lint cannot
see, so the run driver resolves each one before launch (mark the item
``Run (driver):``, or rewrite it with an allowed program).

For every ``Run:`` command, driver items included, a ``/tmp`` or ``/var/tmp``
path is a **problem**: ``/tmp`` is wiped on restart and has lost run data more
than once, so workers and drivers use the worktree's ``.coder-scratch/`` or a
``data_dirs`` path instead.

And for every ``Run:`` command, a repo-relative path it reads must exist or be
declared in some unit's ``files`` — a **problem**. "Reads" means a whole shell
argument (``pytest tests/x.py``) or an ``open('…')`` inside inline code; a path
that is merely data in a literal (``harness_paths=['scripts/eval.py']``) is not
a reference, and a redirect target is an output. Zero LLM, zero codegraph.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from orchestrator.config import _BASE_ALLOWED_TOOLS, PATH_PREFIXES
from orchestrator.grouping.plan_edit import (
    PlanEditError,
    _parse_entry_fields,
    _safe_split_units,
    extract_task_map_entries,
)
from orchestrator.model import is_driver_run

#: A verification bullet's command: ``Run: `cmd``` or ``Run (driver): `cmd```.
_RUN_ITEM = re.compile(r"^\s*-\s*(?P<line>Run(?: \([^)]*\))?:\s*`(?P<cmd>[^`]+)`.*)$", re.MULTILINE)
#: Shell operators the CLI splits a compound command on.
_OPERATORS = {"&&", "||", ";", "|", "&"}
#: A repo-relative file path: at least one directory and a file extension.
_REL_PATH = re.compile(r"\.?[\w-]+(?:/[\w.-]+)+\.[A-Za-z0-9]+")
#: A file an inline program opens: ``open('x')`` / ``open("x")``.
_OPEN_CALL = re.compile(r"open\(\s*['\"](?P<path>[^'\"]+)['\"]")
_REDIRECT_TARGET = re.compile(r"\d?>>?\s*(?P<path>[^\s;|&)'\"]+)")
#: Flags whose value is a path the command writes.
_OUTPUT_FLAGS = {"--out", "--output", "-o", "--out-dir", "--output-dir"}
#: A path under the system temp dirs, which no plan item may use.
_TMP_PATH = re.compile(r"(?<![\w.-])/(?:var/)?tmp(?:/[^\s'\"`;|&)]*)?")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")


def _allowed_programs() -> tuple[set[str], tuple[str, ...]]:
    """Exact program names (``Bash(git *)``) and name prefixes
    (``Bash(pytest*)``) the worker allowlist grants."""
    exact: set[str] = set()
    prefixes: list[str] = []
    for rule in _BASE_ALLOWED_TOOLS:
        if not rule.startswith("Bash(") or not rule.endswith(")"):
            continue
        body = rule[len("Bash(") : -1]
        if " " in body:
            exact.add(body.split(" ", 1)[0])
        elif body.endswith("*"):
            prefixes.append(body[:-1])
        else:
            exact.add(body)
    return exact, tuple(prefixes)


def _segments(cmd: str) -> list[list[str]]:
    """The command's words, split into segments on shell operators outside
    quotes (a ``;`` inside ``python -c "…"`` is not a separator)."""
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes: fall back to one segment
        return [cmd.split()]
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _OPERATORS:
            segments.append([])
        elif token not in ("(", ")"):
            segments[-1].append(token)
    return [seg for seg in segments if seg]


def _program(words: list[str]) -> str | None:
    """The program a segment runs, past env assignments and a known path
    prefix (``.venv/bin/python`` → ``python``)."""
    while words and _ENV_ASSIGN.match(words[0]):
        words = words[1:]
    if not words:
        return None
    program = words[0]
    for prefix in PATH_PREFIXES:
        if program.startswith(prefix):
            return program[len(prefix) :]
    return program


def _redirect_targets(cmd: str) -> set[str]:
    """Paths the command writes by redirect — outputs, not references."""
    return {m.group("path").removeprefix("./") for m in _REDIRECT_TARGET.finditer(cmd)}


def _declared_files(plan_text: str) -> set[str]:
    try:
        doc = extract_task_map_entries(plan_text)
    except PlanEditError:
        return set()
    if doc is None:
        return set()
    files: set[str] = set()
    for task_id in doc.order:
        try:
            fields = _parse_entry_fields(doc.entries[task_id])
        except Exception:  # a malformed entry is validate_plan's to report
            continue
        files.update(str(f) for f in fields.get("files") or [])
    return files


def lint_verification(plan_text: str, repo_root: Path) -> tuple[list[str], list[str]]:
    """``(problems, warnings)`` for the plan's ``Run:`` items, one line each,
    naming the unit. Problems fail ``plan-check``; warnings are printed."""
    exact, prefixes = _allowed_programs()
    declared = _declared_files(plan_text)
    repo = repo_root.resolve()
    problems: list[str] = []
    warnings: list[str] = []
    for unit_id, unit_text in _safe_split_units(plan_text).items():
        for match in _RUN_ITEM.finditer(unit_text):
            cmd = match.group("cmd")
            driver = is_driver_run(match.group("line"))
            if not driver:
                for words in _segments(cmd):
                    program = _program(words)
                    if program is None:
                        continue
                    if program not in exact and not program.startswith(prefixes):
                        warnings.append(
                            f"{unit_id}: `{cmd}` runs `{program}`, which the default "
                            "worker allowlist does not grant — unless your settings "
                            "add it, mark it `Run (driver):` or use an allowed program"
                        )
            tmp = _TMP_PATH.search(cmd)
            if tmp is not None:
                problems.append(
                    f"{unit_id}: `{cmd}` uses {tmp.group(0)} — never /tmp (wiped on "
                    "restart); write to .coder-scratch/ in the worktree or a "
                    "data_dirs path"
                )
            outputs = _redirect_targets(cmd)
            words = [w for seg in _segments(cmd) for w in seg]
            # A directory the command names before it exists is one it creates
            # (`--out docs/runs/x && wc -w docs/runs/x/pr-body.md`): files under
            # it are outputs of an earlier step, not references.
            created = [
                w.removeprefix("./").rstrip("/")
                for i, w in enumerate(words)
                if "/" in w
                and not w.startswith(("-", "/"))
                and (not (repo / w).exists() or (i and words[i - 1] in _OUTPUT_FLAGS))
            ]
            reads = [w for seg in _segments(cmd) for w in seg[1:] if _REL_PATH.fullmatch(w)]
            reads += [m.group("path") for m in _OPEN_CALL.finditer(cmd)]
            for raw in reads:
                rel = raw.removeprefix("./")
                if rel.startswith("/") or rel in declared or rel in outputs:
                    continue
                if any(rel.startswith(d + "/") for d in created):
                    continue
                if (repo / rel).exists():
                    continue
                problems.append(
                    f"{unit_id}: `{cmd}` names {rel}, which neither exists nor is "
                    "declared in any unit's files"
                )
    return list(dict.fromkeys(problems)), list(dict.fromkeys(warnings))
