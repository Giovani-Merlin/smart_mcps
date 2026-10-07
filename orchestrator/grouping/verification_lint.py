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

import keyword
import re
import shlex
import subprocess
from pathlib import Path

from orchestrator.config import _BASE_ALLOWED_TOOLS, PATH_PREFIXES
from orchestrator.grouping.plan_edit import (
    PlanEditError,
    _parse_entry_fields,
    _safe_split_units,
    _unit_key_for_task,
    extract_task_map_entries,
)
from orchestrator.grouping.plan_sections import _split_bullets
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


#: Verbs that make a backticked name the subject of a change in a Goal.
_CHANGE_VERBS = (
    "gains",
    "becomes",
    "now",
    "no longer",
    "extends",
    "accepts",
    "emits",
    "prints",
    "logs",
    "raises",
    "returns",
    "refuses",
    "drops",
    "writes",
    "reads",
    "stops",
    "is",
    "are",
)
_DOTTED_NAME = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
_SUBJECT_VERB = re.compile(
    "`(?P<name>" + _DOTTED_NAME + ")`(?:'s)?\\s+(?P<verb>" + "|".join(_CHANGE_VERBS) + ")\\b"
)
_DEFINITION = re.compile(r"^\s*(?:async\s+def|def|class)\s+(\w+)\b", re.MULTILINE)
_SKIP_DIRS = {".venv", ".worktrees", "node_modules", ".git"}
_MIN_SYMBOL_LEN = 4


def _python_files(repo: Path) -> list[str]:
    """Repo-relative tracked ``*.py`` paths (``git ls-files``, else a walk)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z", "--", "*.py"],
            capture_output=True,
            check=True,
        ).stdout.decode()
        return [p for p in out.split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        return [
            path.relative_to(repo).as_posix()
            for path in repo.rglob("*.py")
            if not _SKIP_DIRS.intersection(path.relative_to(repo).parts)
        ]


def _definitions_index(repo: Path) -> dict[str, set[str]]:
    """Symbol name → repo-relative files that ``def``/``class`` it."""
    index: dict[str, set[str]] = {}
    for rel in _python_files(repo):
        try:
            text = (repo / rel).read_text(errors="replace")
        except OSError:
            continue
        for name in _DEFINITION.findall(text):
            index.setdefault(name, set()).add(rel)
    return index


def _unit_files(plan_text: str) -> dict[str, set[str]]:
    """Unit key (``u7``) → the files its task-map entry declares."""
    try:
        doc = extract_task_map_entries(plan_text)
    except PlanEditError:
        return {}
    if doc is None:
        return {}
    files: dict[str, set[str]] = {}
    for task_id in doc.order:
        key = _unit_key_for_task(task_id)
        if key is None:
            continue
        try:
            fields = _parse_entry_fields(doc.entries[task_id])
        except Exception:  # a malformed entry is validate_plan's to report
            continue
        files.setdefault(key, set()).update(str(f) for f in fields.get("files") or [])
    return files


def lint_goal_symbols(plan_text: str, repo_root: Path) -> list[str]:
    """Warnings for a unit whose Goal changes a symbol (a backticked name
    followed by a change verb) defined only in files outside its ``files``.
    A name defined nowhere is prospective; a bare mention is silent."""
    unit_files = _unit_files(plan_text)
    if not unit_files:
        return []
    index: dict[str, set[str]] | None = None
    warnings: list[str] = []
    for unit_id, unit_text in _safe_split_units(plan_text).items():
        if unit_id not in unit_files:
            continue
        goal = _split_bullets(unit_text).get("Goal", "")
        for match in _SUBJECT_VERB.finditer(goal):
            segments = match.group("name").split(".")
            names = {
                n
                for n in (segments[0], segments[-1])
                if len(n) >= _MIN_SYMBOL_LEN and not keyword.iskeyword(n)
            }
            if not names:
                continue
            if index is None:
                index = _definitions_index(repo_root.resolve())
            defining = set().union(*(index.get(n, set()) for n in names))
            if not defining or defining & unit_files[unit_id]:
                continue
            warnings.append(
                f"{unit_id}: Goal changes {match.group('name')} ({match.group('verb')}), "
                f"defined in {', '.join(sorted(defining))}, which is not in its Files"
            )
    return list(dict.fromkeys(warnings))
