"""``RunFacts`` — the single deterministic source every report format renders
from (plan U1). Everything here is computed from the run directory, the
plan's own text, junit, and git: no LLM call, no invented field.

Two runs' complete fixture sets exercise this end to end —
``tests/fixtures/runs/r20260828-220035`` (11 groups, trouble) and
``tests/fixtures/runs/r20260829-162627`` (3 groups, clean) — see
``tests/test_report_facts.py``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import BaseModel, Field

from orchestrator.execution.export import build_export
from orchestrator.execution.manifest import RunPaths, effective_group
from orchestrator.execution.worktrees import integration_branch
from orchestrator.grouping.plan_sections import UnitSection, parse_plan_sections, unit_key_for_task
from orchestrator.model import Group, GroupingResult, VerificationItem

#: Group lifecycle states counted as "the work landed" (scheduler.TERMINAL_STATES
#: minus FAILED — a resolved group's stranded work still made it to the
#: integration branch).
_LANDED_STATES = frozenset({"completed", "resolved"})

_BULLET_RE = re.compile(r"^-\s+\*\*(?P<label>[^*]+)\*\*:\s?(?P<rest>.*)$")
_FRONTMATTER_RE = re.compile(r"^---\n(?P<body>.*?)\n---\n", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^(?P<key>[a-zA-Z_]+):\s*(?P<value>.*)$")
_OBJECTIVE_RE = re.compile(r"^## Objective[ \t]*$", re.MULTILINE)
_NEXT_H2_RE = re.compile(r"^## [ \t]*\S", re.MULTILINE)
_PUSHED_RE = re.compile(r"finish (?P<run_id>\S+): pushed \S+ to origin at (?P<sha>[0-9a-f]+)")
_MERGE_SUBJECT_RE = re.compile(r"^merge\((?P<run_id>[^)]+)\): (?P<gid>g\d+)\b")
_RID_RE = re.compile(r"R(\d+)")
_UNIT_TOKEN_RE = re.compile(r"U(\d+)")
_DECLARED_RID_RE = re.compile(r"^-\s+R(\d+)\.", re.MULTILINE)


# --------------------------------------------------------------------- model


class GitRangeFacts(BaseModel):
    base_sha: str | None = None
    tip_sha: str | None = None
    available: bool = False


class ChangedFileFacts(BaseModel):
    path: str
    added: int = 0
    deleted: int = 0
    group_id: str | None = None


class SessionFacts(BaseModel):
    role: str
    generation: int = 1
    model: str | None = None
    started_at: str | None = None
    #: The manifest's ``ended_at`` when recorded; otherwise inferred (see
    #: ``ended_at_source``) so elapsed time never silently reads as zero.
    ended_at: str | None = None
    #: ``manifest`` | ``next_session`` (a retired session ends when its
    #: successor starts) | ``heartbeat`` (the group's last heartbeat
    #: ``updated_at``) | ``unknown`` (``ended_at`` is None).
    ended_at_source: str = "manifest"
    retirement_reason: str | None = None
    #: Billable-shaped spend only: ``input``, ``output``, ``cache_creation``.
    #: Cache reads are kept apart in ``cache_read_tokens`` — summing them in
    #: made a one-session group read as 26M "tokens" (report v2 U3).
    tokens: dict[str, int] = Field(default_factory=dict)
    cache_read_tokens: int = 0


def session_tokens(session: "SessionFacts") -> int:
    """Input + output + cache-creation tokens, never cache reads."""
    return sum(session.tokens.values())


class TestFacts(BaseModel):
    ran: bool = False
    total: int = 0
    failures: int = 0
    errors: int = 0
    junit_path: str | None = None


class GroupFacts(BaseModel):
    id: str
    name: str = ""
    #: The manifest's design summary for the group (always available); not
    #: the coder's completion report, which is ``report_summary`` below.
    summary: str = ""
    #: The latest coder report's own ``summary`` field, when one exists
    #: (report U3's per-group fragments trim this to ~20 words).
    report_summary: str | None = None
    #: The spec the coder actually received — the speccer-rewritten one when a
    #: ``spec-gen<N>.json`` exists (``manifest.effective_group``), else the
    #: grouper's original (report v2.1 U4).
    spec: str = ""
    #: The ``merge(<run_id>): <gid>`` commit on the integration branch's
    #: first-parent history; ``None`` when the git range is unavailable or
    #: the group never merged. ``report.html`` shows ``git diff <sha>^1 <sha>``
    #: from it (report v2.1 U3); the diff itself is never stored in facts.
    merge_sha: str | None = None
    state: str = "pending"
    verdict_status: str | None = None
    failure: str | None = None
    stale_failure: bool = False
    tasks: list[str] = Field(default_factory=list)
    planned_files: list[str] = Field(default_factory=list)
    touched_files: list[str] = Field(default_factory=list)
    sessions: list[SessionFacts] = Field(default_factory=list)
    escalations: list[dict] = Field(default_factory=list)
    surprises: list[dict] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)
    #: Parallel to ``required_changes`` — the artifact path (relative to the
    #: run dir) each entry was read from, so a consumer can cite the source.
    required_change_paths: list[str] = Field(default_factory=list)
    tests: TestFacts = Field(default_factory=TestFacts)


class VerificationFacts(BaseModel):
    item_id: str
    description: str
    status: str = "unverified"  # pass | fail | unverified
    evidence: str = ""


class UnitFacts(BaseModel):
    unit_id: str
    task_id: str
    group_id: str | None = None
    title: str = ""
    summary: str = ""
    goal: str = ""
    #: The unit's plan section verbatim — heading line through the next
    #: heading — so the report can show what the plan asked for, not only
    #: its ``Goal`` bullet (report v2.1 U4).
    section_text: str = ""
    verification: list[VerificationFacts] = Field(default_factory=list)
    landed: bool = False


class RidFacts(BaseModel):
    rid: str
    units: list[str] = Field(default_factory=list)
    landed: bool = False


class TimelineEvent(BaseModel):
    at: str
    kind: str
    group_id: str | None = None
    label: str = ""


class AdrDelta(BaseModel):
    path: str
    change: str


class RunFacts(BaseModel):
    run_id: str
    plan_path: str = ""
    plan_title: str = ""
    plan_objective: str = ""
    origin: str = ""
    created_at: str | None = None
    finished_at: str | None = None
    pr_url: str | None = None
    git_range: GitRangeFacts = Field(default_factory=GitRangeFacts)
    changed_files: list[ChangedFileFacts] = Field(default_factory=list)
    groups: list[GroupFacts] = Field(default_factory=list)
    units: list[UnitFacts] = Field(default_factory=list)
    rids: list[RidFacts] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    adr_delta: list[AdrDelta] = Field(default_factory=list)
    trouble: bool = False
    #: ``"base-context.md"`` when the run dir carries the compiled ground
    #: rules + plan every worker received; the text itself stays out of
    #: ``facts.json`` (hundreds of lines) and is read by ``render_html``.
    base_context_path: str | None = None


# ------------------------------------------------------------------- git io


def _run_git(repo_root: Path, *args: str) -> str | None:
    result = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout


def _git_commit_exists(repo_root: Path, sha: str) -> bool:
    return _run_git(repo_root, "cat-file", "-e", f"{sha}^{{commit}}") is not None


def _resolve_tip_sha(repo_root: Path, run_id: str, run_log_text: str) -> str | None:
    for match in _PUSHED_RE.finditer(run_log_text):
        if match.group("run_id") == run_id:
            return match.group("sha")
    out = _run_git(repo_root, "rev-parse", "--verify", integration_branch(run_id))
    return out.strip() if out else None


def _resolve_git_range(paths: RunPaths, repo_root: Path, run_id: str) -> GitRangeFacts:
    base_sha: str | None = None
    baseline_path = paths.preflight_baseline_path
    if baseline_path.is_file():
        import json

        try:
            base_sha = json.loads(baseline_path.read_text()).get("commit_sha")
        except (OSError, ValueError):
            base_sha = None

    run_log_text = ""
    if paths.event_log_path.is_file():
        run_log_text = paths.event_log_path.read_text()
    tip_sha = _resolve_tip_sha(repo_root, run_id, run_log_text)

    available = bool(
        base_sha
        and tip_sha
        and _git_commit_exists(repo_root, base_sha)
        and _git_commit_exists(repo_root, tip_sha)
    )
    return GitRangeFacts(base_sha=base_sha, tip_sha=tip_sha, available=available)


def _merge_commits(
    repo_root: Path, run_id: str, base_sha: str, tip_sha: str
) -> list[tuple[str, str]]:
    """``[(merge_sha, group_id), ...]`` off the integration branch's first-parent
    history — one ``merge(<run_id>): <gid> …`` commit per merged group."""
    log = _run_git(
        repo_root, "log", "--first-parent", "--format=%H\x1f%s", f"{base_sha}..{tip_sha}"
    )
    if log is None:
        return []
    commits: list[tuple[str, str]] = []
    for line in log.splitlines():
        if "\x1f" not in line:
            continue
        sha, subject = line.split("\x1f", 1)
        match = _MERGE_SUBJECT_RE.match(subject)
        if match and match.group("run_id") == run_id:
            commits.append((sha, match.group("gid")))
    return commits


def _numstat(repo_root: Path, base: str, tip: str) -> list[tuple[str, int, int]]:
    out = _run_git(repo_root, "diff", "--numstat", base, tip)
    if out is None:
        return []
    rows: list[tuple[str, int, int]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_s, deleted_s, path = parts
        added = int(added_s) if added_s.isdigit() else 0
        deleted = int(deleted_s) if deleted_s.isdigit() else 0
        rows.append((path, added, deleted))
    return rows


def _planned_files_fallback(groups: list[Group]) -> list[ChangedFileFacts]:
    """Every declared file, attributed to the first group that names it —
    the fallback when the git range is unavailable (report U1)."""
    seen: dict[str, str] = {}
    for group in groups:
        for path in group.files:
            seen.setdefault(path, group.id)
    return [
        ChangedFileFacts(path=path, added=0, deleted=0, group_id=gid) for path, gid in seen.items()
    ]


def _run_merge_commits(
    repo_root: Path, run_id: str, git_range: GitRangeFacts
) -> list[tuple[str, str]]:
    """``_merge_commits`` over the run's git range, or ``[]`` when the range
    is unavailable — computed once in ``build_facts`` and shared by file
    attribution and ``GroupFacts.merge_sha``."""
    if not git_range.available:
        return []
    assert git_range.base_sha is not None and git_range.tip_sha is not None
    return _merge_commits(repo_root, run_id, git_range.base_sha, git_range.tip_sha)


def _build_changed_files(
    repo_root: Path,
    git_range: GitRangeFacts,
    groups: list[Group],
    merges: list[tuple[str, str]],
) -> list[ChangedFileFacts]:
    if not git_range.available:
        return _planned_files_fallback(groups)
    assert git_range.base_sha is not None and git_range.tip_sha is not None
    overall = _numstat(repo_root, git_range.base_sha, git_range.tip_sha)
    if not overall:
        return []
    # Oldest first, so a file touched by more than one group's merge is
    # attributed to the last (most recent) one to have written it.
    path_to_group: dict[str, str] = {}
    for merge_sha, group_id in reversed(merges):
        for path, _added, _deleted in _numstat(repo_root, f"{merge_sha}^1", merge_sha):
            path_to_group[path] = group_id
    return [
        ChangedFileFacts(path=path, added=added, deleted=deleted, group_id=path_to_group.get(path))
        for path, added, deleted in overall
    ]


def _adr_delta(repo_root: Path, git_range: GitRangeFacts) -> list[AdrDelta]:
    if not git_range.available:
        return []
    out = _run_git(
        repo_root,
        "diff",
        "--name-status",
        str(git_range.base_sha),
        str(git_range.tip_sha),
        "--",
        "docs/adr/",
    )
    if not out:
        return []
    deltas = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        deltas.append(AdrDelta(path=parts[-1], change=parts[0]))
    return deltas


# ------------------------------------------------------------------ junit


def _parse_junit_totals(xml_path: Path) -> tuple[int, int, int]:
    """``(total, failures, errors)`` summed across every ``<testsuite>``."""
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return (0, 0, 0)
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = failures = errors = 0
    for suite in suites:
        total += int(suite.get("tests", 0) or 0)
        failures += int(suite.get("failures", 0) or 0)
        errors += int(suite.get("errors", 0) or 0)
    return (total, failures, errors)


def _group_tests(paths: RunPaths, group_id: str) -> TestFacts:
    xml_path = paths.group_dir(group_id) / "preflight-junit.xml"
    if not xml_path.is_file():
        return TestFacts(ran=False)
    total, failures, errors = _parse_junit_totals(xml_path)
    return TestFacts(
        ran=True,
        total=total,
        failures=failures,
        errors=errors,
        junit_path=str(xml_path.relative_to(paths.run_dir)),
    )


# --------------------------------------------------------------- plan text


def _parse_frontmatter(plan_text: str) -> dict[str, str]:
    match = _FRONTMATTER_RE.match(plan_text)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group("body").splitlines():
        field_match = _FRONTMATTER_FIELD_RE.match(line)
        if field_match:
            fields[field_match.group("key")] = field_match.group("value").strip()
    return fields


def _plan_objective(plan_text: str) -> str:
    match = _OBJECTIVE_RE.search(plan_text)
    if not match:
        return ""
    next_h2 = _NEXT_H2_RE.search(plan_text, match.end())
    end = next_h2.start() if next_h2 else len(plan_text)
    return plan_text[match.end() : end].strip()


def _bullet_value(section_text: str, label: str) -> str:
    """The folded value of one ``- **Label**: value`` bullet inside a unit
    section's text (mirrors ``plan_sections._split_bullets`` for the one
    label — ``Goal`` — that module does not itself expose)."""
    collecting = False
    out: list[str] = []
    for line in section_text.splitlines():
        bullet_match = _BULLET_RE.match(line)
        if bullet_match:
            if collecting:
                break
            if bullet_match.group("label").strip() == label:
                collecting = True
                out.append(bullet_match.group("rest"))
            continue
        if collecting:
            out.append(line)
    return "\n".join(out).strip()


def _declared_rids(origin_text: str) -> set[str]:
    return {f"R{n}" for n in _DECLARED_RID_RE.findall(origin_text)}


def _parse_requirement_mentions(plan_text: str) -> dict[str, list[str]]:
    """``{"R16": ["u1", "u2"], ...}`` from any ``R<n>[, R<m>...] → ...U<k>...``
    line in the plan (the ``## Requirement coverage`` convention) — scanned
    without anchoring to a heading so a plan that phrases the section
    differently still yields whatever coverage lines it does have."""
    mapping: dict[str, list[str]] = {}
    for line in plan_text.splitlines():
        if "→" not in line:
            continue
        lhs, _, rhs = line.partition("→")
        rids = [f"R{n}" for n in _RID_RE.findall(lhs)]
        if not rids:
            continue
        units = [f"u{n}" for n in _UNIT_TOKEN_RE.findall(rhs)]
        for rid in rids:
            bucket = mapping.setdefault(rid, [])
            for unit in units:
                if unit not in bucket:
                    bucket.append(unit)
    return mapping


def _attribute_verification(
    group: Group, units: dict[str, UnitSection]
) -> dict[str, list[VerificationItem]]:
    """Slice ``group.verification`` back into per-unit chunks, in the same
    order the assembler concatenated them: each member unit's declared
    ``Run:``/``Pass:`` bullet count is how many of the group's verification
    items are its own."""
    by_task: dict[str, list[VerificationItem]] = {}
    cursor = 0
    for task_id in group.tasks:
        key = unit_key_for_task(task_id)
        unit = units.get(key) if key else None
        count = len(unit.verification) if unit else 0
        by_task[task_id] = group.verification[cursor : cursor + count]
        cursor += count
    return by_task


def _heartbeat_updated_at(paths: RunPaths, group_id: str) -> str | None:
    """The group heartbeat's ``updated_at`` — the last moment the group was
    provably alive, used as the end of a session the manifest never closed."""
    import json

    heartbeat = paths.group_dir(group_id) / "heartbeat.json"
    if not heartbeat.is_file():
        return None
    try:
        value = json.loads(heartbeat.read_text()).get("updated_at")
    except (OSError, ValueError):
        return None
    return value if isinstance(value, str) and value else None


def _session_facts(paths: RunPaths, group_id: str, export_sessions: list) -> list[SessionFacts]:
    """Sessions with the U3 elapsed fallback: a null ``ended_at`` becomes the
    next session's ``started_at`` (a retired session ends when its successor
    starts), else the group heartbeat's ``updated_at`` when that is after the
    start, else stays ``None`` (rendered "n/a", never "0m")."""
    heartbeat_at = _heartbeat_updated_at(paths, group_id)
    facts: list[SessionFacts] = []
    for index, s in enumerate(export_sessions):
        ended_at = s.ended_at
        source = "manifest"
        if not ended_at:
            successor = export_sessions[index + 1] if index + 1 < len(export_sessions) else None
            if successor is not None and successor.started_at:
                ended_at, source = successor.started_at, "next_session"
            elif heartbeat_at and (not s.started_at or heartbeat_at > s.started_at):
                ended_at, source = heartbeat_at, "heartbeat"
            else:
                ended_at, source = None, "unknown"
        tokens = s.tokens.model_dump()
        cache_read = int(tokens.pop("cache_read", 0) or 0)
        facts.append(
            SessionFacts(
                role=s.role,
                generation=s.generation,
                model=s.model,
                started_at=s.started_at,
                ended_at=ended_at,
                ended_at_source=source,
                retirement_reason=s.retirement_reason,
                tokens=tokens,
                cache_read_tokens=cache_read,
            )
        )
    return facts


def _latest_report(paths: RunPaths, group_id: str) -> dict | None:
    import json

    directory = paths.group_dir(group_id)
    if not directory.is_dir():
        return None
    best: tuple[tuple[int, int], Path] | None = None
    for path in directory.glob("report-g*-r*.json"):
        match = re.match(r"^report-g(\d+)-r(\d+)\.json$", path.name)
        if not match:
            continue
        key = (int(match.group(1)), int(match.group(2)))
        if best is None or key > best[0]:
            best = (key, path)
    if best is None:
        return None
    try:
        return json.loads(best[1].read_text())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- assembly


def build_facts(repo_root: Path, run_id: str, *, run_dir: Path | None = None) -> RunFacts:
    paths = RunPaths(repo_root, run_id, run_dir=run_dir)
    export = build_export(paths, project=repo_root.name)

    grouping_result: GroupingResult | None = None
    if paths.groups_path.is_file():
        grouping_result = GroupingResult.model_validate_json(paths.groups_path.read_text())
    groups_by_id: dict[str, Group] = (
        {g.id: g for g in grouping_result.groups} if grouping_result else {}
    )

    plan_path = export.plan.path or (grouping_result.plan_path if grouping_result else "")
    plan_text = ""
    plan_file = repo_root / plan_path if plan_path else None
    if plan_file is not None and plan_file.is_file():
        plan_text = plan_file.read_text()

    frontmatter = _parse_frontmatter(plan_text)
    plan_title = frontmatter.get("title", "")
    origin = frontmatter.get("origin", "")
    plan_objective = _plan_objective(plan_text)

    plan_sections = parse_plan_sections(plan_text) if plan_text else None
    units_by_id: dict[str, UnitSection] = plan_sections.units if plan_sections else {}

    git_range = _resolve_git_range(paths, repo_root, run_id)
    merges = _run_merge_commits(repo_root, run_id, git_range)
    # Newest first in the log; a group that merged twice (resolve after a
    # failed first merge) keeps its most recent merge commit.
    merge_sha_by_group: dict[str, str] = {}
    for merge_sha, gid in merges:
        merge_sha_by_group.setdefault(gid, merge_sha)
    changed_files = _build_changed_files(repo_root, git_range, list(groups_by_id.values()), merges)
    adr_delta = _adr_delta(repo_root, git_range)

    run_log_text = paths.event_log_path.read_text() if paths.event_log_path.is_file() else ""
    pr_url_match = re.search(rf"finish {re.escape(run_id)}: opened PR (\S+)", run_log_text)
    pr_url = pr_url_match.group(1) if pr_url_match else None

    trouble = False
    group_facts: list[GroupFacts] = []
    timeline: list[TimelineEvent] = []
    task_to_group: dict[str, str] = {}
    verification_by_task: dict[str, list[VerificationItem]] = {}

    for export_group in export.groups:
        source_group = groups_by_id.get(export_group.id)
        # The speccer-rewritten spec when one exists: what the coder was
        # actually given, which is what the report should show.
        shown_group = effective_group(paths, source_group) if source_group else None
        tasks = list(source_group.tasks) if source_group else []
        for task_id in tasks:
            task_to_group[task_id] = export_group.id
        if source_group is not None:
            verification_by_task.update(_attribute_verification(source_group, units_by_id))

        sessions = _session_facts(paths, export_group.id, list(export_group.sessions))
        for s in sessions:
            if s.started_at:
                timeline.append(
                    TimelineEvent(
                        at=s.started_at,
                        kind="session_start",
                        group_id=export_group.id,
                        label=s.role,
                    )
                )
            if s.ended_at:
                timeline.append(
                    TimelineEvent(
                        at=s.ended_at, kind="session_end", group_id=export_group.id, label=s.role
                    )
                )
            if s.retirement_reason:
                timeline.append(
                    TimelineEvent(
                        at=s.ended_at or s.started_at or "",
                        kind="retirement",
                        group_id=export_group.id,
                        label=s.retirement_reason,
                    )
                )

        surprises = []
        required_changes: list[str] = []
        required_change_paths: list[str] = []
        verdict_status: str | None = None
        for artifact in export_group.artifacts:
            surprises.extend(
                {
                    "kind": s.kind,
                    "description": s.description,
                    "affected_groups": s.affected_groups,
                    "path": artifact.path,
                }
                for s in artifact.surprises
            )
            for change in artifact.required_changes:
                if change not in required_changes:
                    required_changes.append(change)
                    required_change_paths.append(artifact.path)
            if artifact.kind == "reviewer_verdict" and artifact.status is not None:
                verdict_status = artifact.status

        escalations = [e.model_dump() for e in export_group.escalations]
        for escalation in export_group.escalations:
            timeline.append(
                TimelineEvent(
                    at=escalation.created_at or "",
                    kind="escalation",
                    group_id=export_group.id,
                    label=escalation.kind,
                )
            )

        tests = _group_tests(paths, export_group.id)
        latest_report = _latest_report(paths, export_group.id)
        report_summary = None
        if latest_report is not None:
            raw_summary = latest_report.get("summary")
            if isinstance(raw_summary, str) and raw_summary.strip():
                report_summary = raw_summary.strip()

        real_failure = export_group.failure if not export_group.stale_failure else None
        if (
            real_failure
            or any(s.retirement_reason for s in export_group.sessions)
            or escalations
            or surprises
            or required_changes
        ):
            trouble = True

        group_facts.append(
            GroupFacts(
                id=export_group.id,
                name=shown_group.name if shown_group else export_group.name,
                summary=shown_group.summary if shown_group else export_group.summary,
                report_summary=report_summary,
                spec=shown_group.spec if shown_group else "",
                merge_sha=merge_sha_by_group.get(export_group.id),
                state=export_group.final_state,
                verdict_status=verdict_status,
                failure=real_failure,
                stale_failure=export_group.stale_failure,
                tasks=tasks,
                planned_files=list(source_group.files) if source_group else [],
                touched_files=sorted(
                    {cf.path for cf in changed_files if cf.group_id == export_group.id}
                ),
                sessions=sessions,
                escalations=escalations,
                surprises=surprises,
                required_changes=required_changes,
                required_change_paths=required_change_paths,
                tests=tests,
            )
        )

    group_state_by_id = {g.id: g.state for g in group_facts}

    units: list[UnitFacts] = []
    for unit_id in sorted(units_by_id, key=lambda u: int(u[1:])):
        unit = units_by_id[unit_id]
        task_id = next((t for t in task_to_group if unit_key_for_task(t) == unit_id), unit_id)
        group_id = task_to_group.get(task_id)
        report = _latest_report(paths, group_id) if group_id else None
        results_by_item = {}
        if report:
            for result in report.get("verification_results") or []:
                results_by_item[result.get("item_id")] = result

        items = verification_by_task.get(task_id, [])
        verification_facts: list[VerificationFacts] = []
        for description, item in zip(unit.verification, items):
            result = results_by_item.get(item.id)
            if result is None:
                status = "unverified"
                evidence = ""
            elif result.get("status") == "pass":
                status = "pass"
                evidence = result.get("notes", "")
            elif result.get("status") == "fail":
                status = "fail"
                evidence = result.get("notes", "")
            else:
                status = "unverified"
                evidence = result.get("notes", "")
            verification_facts.append(
                VerificationFacts(
                    item_id=item.id, description=item.description, status=status, evidence=evidence
                )
            )

        group_landed = group_id is not None and group_state_by_id.get(group_id) in _LANDED_STATES
        all_pass = (
            all(v.status == "pass" for v in verification_facts) if verification_facts else True
        )
        landed = group_landed and all_pass

        units.append(
            UnitFacts(
                unit_id=unit_id,
                task_id=task_id,
                group_id=group_id,
                title=unit.title,
                summary=unit.summary,
                goal=_bullet_value(unit.text, "Goal"),
                section_text=unit.text,
                verification=verification_facts,
                landed=landed,
            )
        )

    landed_units = {u.unit_id for u in units if u.landed}
    declared = _declared_rids(
        (repo_root / origin).read_text() if origin and (repo_root / origin).is_file() else ""
    )
    mentions = _parse_requirement_mentions(plan_text)
    rids: list[RidFacts] = []
    for rid, claiming_units in sorted(mentions.items(), key=lambda kv: int(kv[0][1:])):
        if declared and rid not in declared:
            continue
        rids.append(
            RidFacts(
                rid=rid,
                units=claiming_units,
                landed=bool(claiming_units) and all(u in landed_units for u in claiming_units),
            )
        )

    return RunFacts(
        run_id=run_id,
        plan_path=plan_path,
        plan_title=plan_title,
        plan_objective=plan_objective,
        origin=origin,
        created_at=export.created_at,
        finished_at=None,
        pr_url=pr_url,
        git_range=git_range,
        changed_files=changed_files,
        groups=group_facts,
        units=units,
        rids=rids,
        timeline=sorted(timeline, key=lambda e: e.at),
        adr_delta=adr_delta,
        trouble=trouble,
        base_context_path=(
            "base-context.md" if (paths.run_dir / "base-context.md").is_file() else None
        ),
    )
