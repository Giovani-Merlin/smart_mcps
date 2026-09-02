"""Push the integration branch, open a ready-for-review PR, and tear down
exactly what is provably merged (plan U8/U9).

Teardown gates on *completeness*, not worktree state: the CLAUDE.md rule
"never clean a crashed group's uncommitted worktree progress" is about
completeness — while the plan is unfinished, a crashed group's worktree may be
the only copy of work a `retry` will build on. Once a group's branch is an
ancestor of the integration tip, that work is banked in the integration
branch's own history, and the worktree is safe to remove. ``git branch -d``
(never ``-D``) is the second, independent guard behind that ancestry check.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from orchestrator.config import ExecutionConfig, load_config
from orchestrator.execution.manifest import (
    ManifestStore,
    RunPaths,
    archive_review_scratch,
    effective_group,
)
from orchestrator.execution.prompting import REVIEW_SCRATCH_DIRNAME
from orchestrator.execution.review import format_residue_report, surprise_residue
from orchestrator.execution.scheduler import GroupState, RunState
from orchestrator.execution.worktrees import (
    _branch_exists,
    _git,
    _git_ok,
    ensure_excluded,
    existing_worktree_path,
    group_branch,
    integration_branch,
    is_dirty,
    remove_worktree,
    worktree_path,
)
from orchestrator.model import GroupingResult, RunManifest


class FinishError(Exception):
    """`finish` could not complete a required step (e.g. the push failed)."""


@dataclass
class FinishResult:
    integration_branch: str
    integration_sha: str
    pr_url: str | None
    pr_skip_reason: str | None
    # Groups not provably merged (not completed/resolved, or not an ancestor
    # of the integration tip): their worktree and branch are left untouched.
    unmerged: list[str] = field(default_factory=list)
    # Groups whose worktree was torn down but whose branch survived because
    # git's own `-d` merge check refused it (plan R33) — reported, not forced.
    kept_branches: list[str] = field(default_factory=list)


def run_is_finishable(repo_root: Path, run_id: str) -> tuple[bool, list[str]]:
    """Whether every group ended COMPLETED/RESOLVED *and* its branch is an
    ancestor of the integration tip (plan U8 Decisions) — the gate the run
    itself checks before invoking `finish` on its own. Returns ``(ok, bad)``
    where ``bad`` names every group failing either check."""
    paths = RunPaths(repo_root, run_id)
    state = RunState.model_validate_json(paths.state_path.read_text())
    branch = integration_branch(run_id)
    tip = _git_ok(repo_root, "rev-parse", branch).strip()
    bad = [
        gid
        for gid in sorted(state.groups)
        if not _group_is_merged(repo_root, run_id, tip, gid, state.groups[gid])
    ]
    return (not bad, bad)


def _group_is_merged(repo_root: Path, run_id: str, tip: str, gid: str, entry) -> bool:
    if entry.state not in (GroupState.COMPLETED, GroupState.RESOLVED):
        return False
    branch = group_branch(run_id, gid)
    if not _branch_exists(repo_root, branch):
        return False
    return _is_ancestor(repo_root, branch, tip)


def finish_run(
    repo_root: Path,
    run_id: str,
    *,
    log: Callable[[str], None] | None = None,
    announce: Callable[[str], None] | None = None,
) -> FinishResult:
    """Push the integration branch, open a ready-for-review PR against the
    run's launch branch, then tear down every group provably merged into it
    (plan U8/U9).

    ``log`` is the run's own event log (best-effort, silent by default in
    tests); ``announce`` is user-facing stdout (defaults to ``print``).
    """
    log = log or (lambda _text: None)
    announce = announce or print
    paths = RunPaths(repo_root, run_id)
    if not paths.state_path.is_file():
        raise FinishError(f"no run state at {paths.state_path}")
    state = RunState.model_validate_json(paths.state_path.read_text())
    store = ManifestStore(paths)
    manifest = store.load() if store.exists() else None

    branch = integration_branch(run_id)
    # Resolved, not constructed: a run started before U2's run-scoping has its
    # integration worktree at the legacy path, and `git branch -d` has to run
    # with HEAD at the integration tip for its merge check to mean anything —
    # the repo root would check against the launch branch and refuse everything.
    integration_wt = existing_worktree_path(repo_root, run_id, "integration", "integration")
    if integration_wt is None:
        raise FinishError(
            f"integration worktree for {run_id} not found at "
            f"{worktree_path(repo_root, run_id, 'integration', 'integration')} "
            "or its legacy path; branch teardown needs it to verify merges"
        )

    _generate_and_commit_docs(repo_root, run_id, integration_wt, paths, log)

    tip = _git_ok(repo_root, "rev-parse", branch).strip()
    _push_integration_branch(repo_root, run_id)
    log(f"finish {run_id}: pushed {branch} to origin at {tip}")

    launch_branch = manifest.launch_branch if manifest is not None else None
    pr_url: str | None = None
    pr_skip_reason: str | None = None
    if launch_branch is None:
        pr_skip_reason = "run was launched from a detached HEAD"
    else:
        body = _render_pr_body(repo_root, run_id, tip, state, manifest, paths)
        ok, result = _open_pr(repo_root, run_id, launch_branch, body)
        if ok:
            pr_url = result
        else:
            pr_skip_reason = result

    if pr_url is not None:
        log(f"finish {run_id}: opened PR {pr_url}")
        announce(f"opened PR: {pr_url}")
    else:
        message = (
            f"integration branch {branch} is ready at {tip}; could not open a PR ({pr_skip_reason})"
        )
        log(f"finish {run_id}: {message}")
        announce(message)

    unmerged: list[str] = []
    kept_branches: list[str] = []
    for gid in sorted(state.groups):
        entry = state.groups[gid]
        if not _group_is_merged(repo_root, run_id, tip, gid, entry):
            unmerged.append(gid)
            continue
        _teardown_group(repo_root, run_id, gid, paths)
        gbranch = group_branch(run_id, gid)
        if not _delete_branch_if_merged(integration_wt, gbranch):
            kept_branches.append(gid)
            log(f"finish {run_id}: kept branch {gbranch} — git considers it unmerged")

    if unmerged:
        announce(f"kept (not provably merged into {branch}): {', '.join(unmerged)}")
    if kept_branches:
        announce(
            f"worktree removed but branch kept (git considers unmerged): {', '.join(kept_branches)}"
        )

    residue = surprise_residue(paths, state)
    announce(format_residue_report(residue))
    if residue:
        log(f"finish {run_id}: {len(residue)} surprise bucket(s) never delivered")

    return FinishResult(
        integration_branch=branch,
        integration_sha=tip,
        pr_url=pr_url,
        pr_skip_reason=pr_skip_reason,
        unmerged=unmerged,
        kept_branches=kept_branches,
    )


# --------------------------------------------------------------------- push


def _push_integration_branch(repo_root: Path, run_id: str) -> None:
    branch = integration_branch(run_id)
    result = subprocess.run(
        ["git", "push", "origin", f"{branch}:{branch}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise FinishError(f"push of {branch} to origin failed: {result.stderr.strip()[:500]}")


# -------------------------------------------------------------------- docs


def _render_facts_doc(facts, out_dir: Path) -> None:
    from orchestrator.execution.manifest import atomic_write_text

    atomic_write_text(out_dir / "facts.json", facts.model_dump_json(indent=2) + "\n")


def _render_html_doc(facts, out_dir: Path, repo_root: Path) -> None:
    from orchestrator.execution.manifest import atomic_write_text
    from orchestrator.report.diagrams import render_all
    from orchestrator.report.html import render_html

    diagrams = render_all(facts, repo_root)
    atomic_write_text(out_dir / "report.html", render_html(facts, diagrams))


def _render_changelog_doc(facts, out_dir: Path, repo_root: Path) -> None:
    # Deliberately not `render.markdown`'s `update_runlog` side effect: that
    # writes `docs/RUNLOG.md`, outside `docs/runs/<run_id>/`, and `finish`
    # must never commit anything outside that directory (plan U5 constraint).
    from orchestrator.execution.manifest import atomic_write_text
    from orchestrator.report.diagrams import render_all
    from orchestrator.report.markdown import render_changelog_entry

    diagrams = render_all(facts, repo_root)
    atomic_write_text(out_dir / "CHANGELOG-entry.md", render_changelog_entry(facts, diagrams))


def _render_pr_body_doc(facts, out_dir: Path) -> None:
    from orchestrator.execution.manifest import atomic_write_text
    from orchestrator.report.markdown import render_pr_body

    atomic_write_text(out_dir / "pr-body.md", render_pr_body(facts))


#: format name -> renderer, scoped to writing only inside `docs/runs/<run_id>/`
#: — the subset of the CLI's report formats that have no side effect outside
#: that directory (plan U5; `report.render`-style registry, kept local here
#: rather than shared with the CLI's `_REPORT_FORMATS` because the CLI's
#: `changelog` renderer intentionally also updates `docs/RUNLOG.md`).
_DOCS_FORMATS: dict[str, Callable] = {
    "facts": lambda facts, out_dir, repo_root: _render_facts_doc(facts, out_dir),
    "html": _render_html_doc,
    "changelog": _render_changelog_doc,
    "pr-body": lambda facts, out_dir, repo_root: _render_pr_body_doc(facts, out_dir),
}


def _generate_and_commit_docs(
    repo_root: Path,
    run_id: str,
    integration_wt: Path,
    paths: RunPaths,
    log: Callable[[str], None],
) -> None:
    """Render every ``[docs] formats`` entry into ``docs/runs/<run_id>/`` on
    the integration worktree, validate a present ``one-pager.md`` against
    this run's facts, then commit — all before `finish` pushes, so the pushed
    tip already carries the report (plan U5). A config with no formats is a
    no-op: a run against a foreign repo gets no unsolicited `docs/` commit.
    """
    config = load_config(repo_root / ".orchestrator" / "config.toml")
    if not config.docs.formats:
        return

    from orchestrator.report.facts import build_facts

    facts = build_facts(repo_root, run_id, run_dir=paths.run_dir)
    out_dir = integration_wt / config.docs.out_dir / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    for name in config.docs.formats:
        renderer = _DOCS_FORMATS.get(name)
        if renderer is None:
            raise FinishError(
                f"unknown [docs] format {name!r}; expected one of {sorted(_DOCS_FORMATS)}"
            )
        renderer(facts, out_dir, repo_root)

    one_pager_path = out_dir / "one-pager.md"
    if one_pager_path.is_file():
        from orchestrator.report.onepager import validate

        violations = validate(one_pager_path.read_text(), facts)
        if violations:
            raise FinishError(
                f"one-pager.md failed validation for {run_id}:\n"
                + "\n".join(f"- {v}" for v in violations)
            )

    rel_out_dir = out_dir.relative_to(integration_wt).as_posix()
    _git_ok(integration_wt, "add", rel_out_dir)
    status = _git_ok(integration_wt, "status", "--porcelain", "--", rel_out_dir)
    if not status.strip():
        return
    _git_ok(integration_wt, "commit", "-m", f"docs(run): report for {run_id}")
    log(f"finish {run_id}: committed {rel_out_dir}")


# ----------------------------------------------------------------------- PR


def _render_pr_body(
    repo_root: Path,
    run_id: str,
    tip: str,
    state: RunState,
    manifest: RunManifest | None,
    paths: RunPaths,
) -> str:
    """The PR body, rendered from ``RunFacts`` via ``report.markdown`` (plan
    U3) — fixed headings (Motivation/Changes/Risks/Testing/Handoff, plus a
    Postmortem when the run had trouble), never free narrative."""
    from orchestrator.report.facts import build_facts
    from orchestrator.report.markdown import render_pr_body

    facts = build_facts(repo_root, run_id, run_dir=paths.run_dir)
    return render_pr_body(facts)


def _open_pr(repo_root: Path, run_id: str, launch_branch: str, body: str) -> tuple[bool, str]:
    """Open a ready-for-review PR for the integration branch against
    ``launch_branch``.

    Returns ``(True, pr_url)`` on success or ``(False, reason)`` — a missing,
    unauthenticated, or non-GitHub `gh` never raises (plan R30): the caller
    treats a False result as "continue into cleanup, exit zero".
    """
    if shutil.which("gh") is None:
        return False, "gh is not installed"
    auth = subprocess.run(["gh", "auth", "status"], cwd=repo_root, capture_output=True, text=True)
    if auth.returncode != 0:
        return False, "gh is not authenticated"
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=repo_root, capture_output=True, text=True
    )
    if remote.returncode != 0 or "github.com" not in remote.stdout:
        return False, "origin is not a GitHub remote"
    branch = integration_branch(run_id)
    result = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            launch_branch,
            "--head",
            branch,
            "--title",
            f"orchestrator run {run_id}",
            "--body-file",
            "-",
        ],
        cwd=repo_root,
        input=body,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, (result.stderr.strip()[:300] or "gh pr create failed")
    return True, result.stdout.strip()


# ------------------------------------------------------------------ ancestry


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# -------------------------------------------------------------- teardown


def _group_name(paths: RunPaths, group_id: str) -> str:
    result = GroupingResult.model_validate_json(paths.groups_path.read_text())
    for group in result.groups:
        if group.id == group_id:
            # A speccer-rewritten group's worktree is slugged from the rewritten
            # name; groups.json keeps the grouper's original, so resolving it
            # bare would make teardown no-op and leak the worktree.
            return effective_group(paths, group).name
    raise FinishError(f"group {group_id} not found in {paths.groups_path}")


def _teardown_group(repo_root: Path, run_id: str, gid: str, paths: RunPaths) -> None:
    """Archive remaining scratch, write a leftover patch for any uncommitted
    change, then force-remove the worktree (plan U9). A no-op when the
    worktree is already gone."""
    # Legacy-aware for the same reason as the integration worktree above:
    # constructing the run-scoped path would silently no-op on a pre-U2 run and
    # leave its group worktrees behind forever.
    worktree = existing_worktree_path(repo_root, run_id, gid, _group_name(paths, gid))
    if worktree is None:
        return
    scratch_dir = worktree / REVIEW_SCRATCH_DIRNAME
    if scratch_dir.exists():
        ensure_excluded(worktree, REVIEW_SCRATCH_DIRNAME)
        archive_review_scratch(
            scratch_dir,
            paths.review_scratch_archive_dir(gid),
            cap_bytes=ExecutionConfig().review_scratch_cap_bytes,
            log=None,
        )
    if is_dirty(worktree):
        _write_leftover_patch(worktree, paths.group_dir(gid) / "leftover.patch")
    remove_worktree(repo_root, worktree, force=True)


def _write_leftover_patch(worktree: Path, dest: Path) -> None:
    """A patch of every uncommitted change (tracked and untracked), written
    before the worktree that produced it is force-removed (plan R32). Staging
    everything is safe here — the worktree is about to be deleted regardless —
    and it is what makes untracked files show up in the diff at all."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(worktree, "add", "-A")
    diff = _git(worktree, "diff", "--cached").stdout
    dest.write_text(diff)


def _delete_branch_if_merged(integration_wt: Path, branch: str) -> bool:
    """``git branch -d``, run from the integration worktree so git's own merge
    check is against the integration branch's own HEAD (plan U9 Decisions) —
    the second, independent guard behind the ancestry check already performed.
    Never ``-D``: a branch git itself considers unmerged survives untouched."""
    result = _git(integration_wt, "branch", "-d", branch)
    return result.returncode == 0
