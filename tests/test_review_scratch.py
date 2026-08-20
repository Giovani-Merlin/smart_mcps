"""U6 tests: the reviewer's scratch directory is named once, excluded per
worktree (never the tracked .gitignore), and archived out at round end under a
configurable cap."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orchestrator.config import BreakerConfig, ExecutionConfig
from orchestrator.execution.manifest import ManifestStore, RunPaths, archive_review_scratch
from orchestrator.execution.prompting import REVIEW_SCRATCH_DIRNAME, render_reviewer_prompt
from orchestrator.execution.review import ReviewDeps, SurpriseBoard, make_executor
from orchestrator.execution.scheduler import GroupContext
from orchestrator.execution.sessions import RoundResult, RoundUsage
from orchestrator.execution.worktrees import ensure_excluded
from orchestrator.model import Group, ReviewIntensity, RunManifest, VerificationItem


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    git(wt, "init", "-b", "main")
    git(wt, "config", "user.email", "t@t")
    git(wt, "config", "user.name", "t")
    git(wt, "commit", "--allow-empty", "-m", "init")
    return wt


def make_group(gid: str = "g1", **overrides) -> Group:
    defaults = dict(
        id=gid,
        name=f"group {gid}",
        summary=f"summary {gid}",
        spec="spec v1",
        difficulty=0.5,
        intensity=ReviewIntensity.PAIRED,
        verification=[VerificationItem(id="v1", description="tests pass")],
    )
    defaults.update(overrides)
    return Group(**defaults)


# ---------------------------------------------------------------- the prompt


def test_prompt_names_exactly_one_scratch_path_inside_the_worktree(worktree):
    group = make_group()
    scratch = str(worktree / REVIEW_SCRATCH_DIRNAME)
    prompt = render_reviewer_prompt(
        "r1", group, report_path="report.json", base_ref="main", scratch_dir=scratch
    )
    assert prompt.count(scratch) == 1
    assert scratch.startswith(str(worktree))


# --------------------------------------------------------- exclude + archive


def test_ensure_excluded_is_idempotent_and_never_touches_gitignore(worktree):
    (worktree / ".gitignore").write_text("*.pyc\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-m", "add gitignore")

    ensure_excluded(worktree, REVIEW_SCRATCH_DIRNAME)
    ensure_excluded(worktree, REVIEW_SCRATCH_DIRNAME)  # idempotent

    exclude_path = Path(git(worktree, "rev-parse", "--git-path", "info/exclude").strip())
    if not exclude_path.is_absolute():
        exclude_path = worktree / exclude_path
    lines = exclude_path.read_text().splitlines()
    assert lines.count(REVIEW_SCRATCH_DIRNAME) == 1
    assert (worktree / ".gitignore").read_text() == "*.pyc\n"


def test_archive_moves_files_out_and_leaves_worktree_clean(worktree):
    scratch = worktree / REVIEW_SCRATCH_DIRNAME
    scratch.mkdir()
    (scratch / "notes.txt").write_text("scratch notes\n")
    (scratch / "nested").mkdir()
    (scratch / "nested" / "deep.txt").write_text("deep\n")

    dest = worktree.parent / "archive"
    archive_review_scratch(scratch, dest, cap_bytes=10_000)

    assert not scratch.exists()
    assert (dest / "notes.txt").read_text() == "scratch notes\n"
    assert (dest / "nested" / "deep.txt").read_text() == "deep\n"
    assert git(worktree, "status", "--porcelain").strip() == ""


def test_archive_no_op_when_scratch_dir_absent(tmp_path):
    dest = tmp_path / "archive"
    archive_review_scratch(tmp_path / "does-not-exist", dest, cap_bytes=10_000)
    assert not dest.exists()


def test_cap_skips_and_names_files_beyond_it_and_still_removes_them(worktree):
    scratch = worktree / REVIEW_SCRATCH_DIRNAME
    scratch.mkdir()
    (scratch / "small.txt").write_text("a" * 10)
    (scratch / "big.txt").write_text("b" * 100)

    dest = worktree.parent / "archive"
    logged = []
    archive_review_scratch(scratch, dest, cap_bytes=50, log=logged.append)

    assert (dest / "small.txt").is_file()
    assert not (dest / "big.txt").exists()
    skipped_text = (dest / "skipped.txt").read_text()
    assert "big.txt" in skipped_text and "100" in skipped_text
    assert not scratch.exists()  # litter removed either way
    assert any("skipped" in line for line in logged)


def test_default_cap_is_100mb():
    assert ExecutionConfig().review_scratch_cap_bytes == 100_000_000


# --------------------------------------------------- wired through the loop


def verdict(status: str = "approved") -> str:
    body = {"status": status, "required_changes": [], "surprises": [], "notes": ""}
    return f'<run-report status="{status}">\n{json.dumps(body)}\n</run-report>'


def coder_report(status: str = "completed") -> str:
    body = {
        "status": status,
        "summary": "done",
        "verification_results": [{"item_id": "v1", "status": "pass", "notes": ""}],
        "surprises": [],
    }
    return f'<run-report status="{status}">\n{json.dumps(body)}\n</run-report>'


class ScratchWritingRunner:
    """Plays a coder round then a reviewer round; the reviewer round writes a
    file into the scratch directory before returning its verdict, mimicking a
    real reviewer session using the scratch path it was told about."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.model = "stub-model"
        self._counter = 0
        self._roles: dict[str, str] = {}

    def start_fork(
        self, *, base_id, prompt, name, cwd, session_id=None, json_schema=None, on_turn=None
    ) -> RoundResult:
        self._counter += 1
        sid = session_id or f"sess-{self._counter}"
        role = "reviewer" if "reviewer" in name else "coder"
        self._roles[sid] = role
        if role == "reviewer":
            scratch = self.workspace / REVIEW_SCRATCH_DIRNAME
            scratch.mkdir(exist_ok=True)
            (scratch / "notes.txt").write_text("reviewer scratch\n")
            text = verdict("approved")
        else:
            text = coder_report("completed")
        return RoundResult(session_id=sid, text=text, usage=RoundUsage(), envelope={})

    def resume(self, *, session_id, prompt, cwd, json_schema=None, on_turn=None) -> RoundResult:
        raise AssertionError("not used in this scenario")

    def effective_disallowed_tools(self) -> list[str]:
        return []

    def usage_of(self, session_id: str):
        from orchestrator.execution.sessions import SessionUsage

        return SessionUsage(last_context_tokens=1_000)

    def transcript_path(self, session_id: str) -> Path | None:
        return None


def test_round_end_archives_scratch_and_worktree_stays_clean_for_merge(tmp_path, worktree):
    group = make_group()
    runner = ScratchWritingRunner(worktree)
    store = ManifestStore(RunPaths(tmp_path, "r1"))
    manifest = RunManifest(run_id="r1", plan_path="p.md", base_session_id="base-0")
    merged: list[str] = []

    def merge_group(g: Group, ws: Path) -> None:
        merged.append(g.id)

    deps = ReviewDeps(
        run_id="r1",
        runner=runner,
        store=store,
        manifest=manifest,
        base_session_id="base-0",
        breaker=BreakerConfig(),
        execution=ExecutionConfig(),
        board=SurpriseBoard(),
        workspace_for=lambda g: worktree,
        merge_group=merge_group,
        rewrite_spec=lambda g, surprises: g,
        base_ref_for=lambda g: "main",
    )
    ctx = GroupContext(
        group=group, generation=1, set_state=lambda s: None, set_generation=lambda g: None
    )
    import asyncio

    final_state = asyncio.run(make_executor(deps)(ctx))

    from orchestrator.execution.scheduler import GroupState

    assert final_state == GroupState.COMPLETED
    assert merged == ["g1"]
    assert not (worktree / REVIEW_SCRATCH_DIRNAME).exists()
    archived = RunPaths(tmp_path, "r1").review_scratch_archive_dir("g1")
    assert (archived / "notes.txt").read_text() == "reviewer scratch\n"
    assert git(worktree, "status", "--porcelain").strip() == ""
