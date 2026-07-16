"""U5 tests: session runner, report rounds, manifest, worktrees (plan Phase B).

Every test runs against tests/fake_claude.py — zero live CLI calls, zero tokens
(plan R24). The stub speaks the envelope shapes the U5 spike verified.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from orchestrator.execution.manifest import (
    ManifestStore,
    RunPaths,
    artifact_name,
    record_session,
)
from orchestrator.execution.prompting import (
    render_coder_prompt,
    render_handoff_prompt,
    render_reviewer_prompt,
)
from orchestrator.execution.sessions import (
    PreflightError,
    ReportError,
    RoundTimeout,
    SessionError,
    SessionRunner,
    nudge_until_report,
    parse_report,
    session_display_name,
)
from orchestrator.execution.worktrees import (
    WorktreeError,
    create_worktree,
    group_branch,
    integration_branch,
    remove_worktree,
    worktree_path,
)
from orchestrator.model import (
    CoderReport,
    Group,
    ReviewerVerdict,
    ReviewIntensity,
    RunManifest,
    SessionEntry,
    SessionRole,
    VerificationItem,
)

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"

# Injected-prefix patterns infinity-skills drops as harness noise; the identity
# block must never look like one (infinity-skills-analysis.md §6 rec 7).
INJECTED_PREFIXES = ("<command-", "<system-reminder", "<local-command", "Caveat:")


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "fake-claude"
    (home / "sessions").mkdir(parents=True)
    return home


def make_runner(fake_home: Path, **kwargs) -> SessionRunner:
    env = {"FAKE_CLAUDE_HOME": str(fake_home), **kwargs.pop("env", {})}
    kwargs.setdefault("timeout_s", 20.0)
    kwargs.setdefault("transcript_root", fake_home / "projects")
    return SessionRunner(claude_bin=[sys.executable, str(FAKE_CLAUDE)], env=env, **kwargs)


def script(fake_home: Path, *entries: dict) -> None:
    with (fake_home / "script.jsonl").open("a") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def calls(fake_home: Path) -> list[dict]:
    path = fake_home / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def report_block(status: str = "completed", **overrides) -> str:
    body = {
        "status": status,
        "summary": "did the work",
        "verification_results": [],
        "surprises": [],
        **overrides,
    }
    return f'prose first\n\n<run-report status="{status}">\n{json.dumps(body)}\n</run-report>'


def make_group(gid: str = "g1", **overrides) -> Group:
    defaults = dict(
        id=gid,
        name="fix auth flow",
        summary="Repair the token refresh path in the auth service.",
        spec="Refactor refresh() to retry once on 401 and add tests.",
        difficulty=0.4,
        intensity=ReviewIntensity.PAIRED,
        verification=[
            VerificationItem(id="v1", description="unit tests pass"),
            VerificationItem(id="v2", description="no new lint errors", required=False),
        ],
    )
    defaults.update(overrides)
    return Group(**defaults)


# ---------------------------------------------------------------- preflight


def test_preflight_passes_on_full_featured_cli(fake_home):
    make_runner(fake_home).preflight()


def test_preflight_fails_naming_the_missing_flag_and_version(fake_home):
    runner = make_runner(fake_home, env={"FAKE_CLAUDE_HIDE_FLAGS": "--fork-session"})
    with pytest.raises(PreflightError, match=r"fake.*--fork-session"):
        runner.preflight()


# ---------------------------------------------------------------- sessions


def test_start_base_pre_assigns_uuid_and_sets_display_name(fake_home, tmp_path):
    runner = make_runner(fake_home)
    result = runner.start_base(run_id="run1", base_context="shared context", cwd=tmp_path)
    uuid.UUID(result.session_id)  # valid UUID
    (call,) = calls(fake_home)
    assert call["argv"][call["argv"].index("--session-id") + 1] == result.session_id
    assert call["argv"][call["argv"].index("--name") + 1] == "run1-base"
    assert "shared context" in call["prompt"]


def test_fork_records_parent_base_and_every_id_is_unique(fake_home, tmp_path):
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="run1", base_context="ctx", cwd=tmp_path)
    fork_ids = set()
    for i in range(4):
        fork = runner.start_fork(
            base_id=base.session_id, prompt="go", name=f"run1-g{i}-coder-g1", cwd=tmp_path
        )
        fork_ids.add(fork.session_id)
        parent = json.loads((fake_home / "sessions" / f"{fork.session_id}.json").read_text())
        assert parent["parent"] == base.session_id
    assert len(fork_ids) == 4  # UUID reuse would silently merge analyzer rows
    assert base.session_id not in fork_ids


def test_sessions_launch_with_convention_display_names(fake_home, tmp_path):
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    name = session_display_name("r1", "g2", "reviewer", 3)
    assert name == "r1-g2-reviewer-g3"
    runner.start_fork(base_id=base.session_id, prompt="go", name=name, cwd=tmp_path)
    fork_call = calls(fake_home)[-1]
    assert fork_call["argv"][fork_call["argv"].index("--name") + 1] == name


def test_resume_of_unknown_session_raises(fake_home, tmp_path):
    runner = make_runner(fake_home)
    with pytest.raises(SessionError, match="No conversation found"):
        runner.resume(session_id=str(uuid.uuid4()), prompt="hi", cwd=tmp_path)


def test_usage_accumulates_across_rounds_and_is_queryable_per_session(fake_home, tmp_path):
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    script(
        fake_home,
        {"usage": {"input_tokens": 5, "output_tokens": 10, "cache_read_input_tokens": 1000}},
        {"usage": {"input_tokens": 7, "output_tokens": 20, "cache_read_input_tokens": 2000}},
    )
    fork = runner.start_fork(base_id=base.session_id, prompt="a", name="n", cwd=tmp_path)
    runner.resume(session_id=fork.session_id, prompt="b", cwd=tmp_path)
    usage = runner.usage_of(fork.session_id)
    assert usage.rounds == 2
    assert usage.total_output_tokens == 30
    # last-round context = input + output + cache_read + cache_creation (spike finding)
    assert usage.last_context_tokens == 7 + 20 + 2000 + 200
    assert runner.usage_of(base.session_id).rounds == 1


def test_round_timeout_kills_the_subprocess_and_raises(fake_home, tmp_path):
    runner = make_runner(fake_home, timeout_s=0.5)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    script(fake_home, {"delay_s": 5})
    with pytest.raises(RoundTimeout):
        runner.resume(session_id=base.session_id, prompt="slow", cwd=tmp_path)


def test_scripted_cli_failure_surfaces_stderr(fake_home, tmp_path):
    runner = make_runner(fake_home)
    script(fake_home, {"exit_code": 2, "stderr": "boom"})
    with pytest.raises(SessionError, match="boom"):
        runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)


def test_transcript_path_is_discoverable_by_session_uuid(fake_home, tmp_path):
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    path = runner.transcript_path(base.session_id)
    assert path is not None and path.name == f"{base.session_id}.jsonl"


def test_concurrent_fork_requests_execute_serially(fake_home, tmp_path):
    runner = make_runner(fake_home, env={"FAKE_CLAUDE_DEFAULT_DELAY_S": "0"})
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    runner._env["FAKE_CLAUDE_DEFAULT_DELAY_S"] = "0.3"
    threads = [
        threading.Thread(
            target=runner.start_fork,
            kwargs=dict(base_id=base.session_id, prompt="go", name=f"n{i}", cwd=tmp_path),
        )
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not (fake_home / "fork_overlaps.log").exists()
    forks = [c for c in calls(fake_home) if "--fork-session" in c["argv"]]
    assert len(forks) == 2
    first, second = sorted(forks, key=lambda c: c["ts_start"])
    assert first["ts_end"] <= second["ts_start"]


# ---------------------------------------------------------------- reports


def test_parse_report_takes_the_last_block_and_accepts_fences(fake_home):
    text = (
        report_block("blocked")
        + '\n<run-report status="completed">\n```json\n'
        + json.dumps({"status": "completed", "summary": "s"})
        + "\n```\n</run-report>"
    )
    report = parse_report(text, CoderReport)
    assert report.status == "completed"


def test_parse_report_rejects_missing_block_and_bad_schema(fake_home):
    with pytest.raises(ReportError, match="no <run-report>"):
        parse_report("all done!", CoderReport)
    bad = '<run-report status="nope">{"status": "nope"}</run-report>'
    with pytest.raises(ReportError, match="failed validation"):
        parse_report(bad, CoderReport)


def test_parse_report_works_for_reviewer_verdicts(fake_home):
    text = (
        '<run-report status="changes_required">'
        '{"status": "changes_required", "required_changes": ["fix x"]}'
        "</run-report>"
    )
    verdict = parse_report(text, ReviewerVerdict)
    assert verdict.status == "changes_required"
    assert verdict.required_changes == ["fix x"]


def test_invalid_report_gets_exactly_n_renudges_then_fails(fake_home, tmp_path):
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    script(fake_home, {"result": "no block 1"}, {"result": "no block 2"}, {"result": "no block 3"})
    first = runner.start_fork(base_id=base.session_id, prompt="go", name="n", cwd=tmp_path)
    with pytest.raises(ReportError, match="after 2 re-nudges"):
        nudge_until_report(runner, first, CoderReport, cwd=tmp_path, max_nudges=2)
    # base + fork + exactly 2 nudge resumes — no more, no fewer
    assert len(calls(fake_home)) == 4


def test_renudge_recovers_when_a_later_round_produces_a_report(fake_home, tmp_path):
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    script(fake_home, {"result": "forgot the block"}, {"result": report_block("completed")})
    first = runner.start_fork(base_id=base.session_id, prompt="go", name="n", cwd=tmp_path)
    report, final = nudge_until_report(runner, first, CoderReport, cwd=tmp_path, max_nudges=2)
    assert report.status == "completed"
    assert final.session_id == first.session_id  # same warm session, no respawn


# ---------------------------------------------------------------- manifest


def test_manifest_records_every_session_with_role_generation_name_summary(tmp_path):
    paths = RunPaths(tmp_path, "run1")
    store = ManifestStore(paths)
    manifest = RunManifest(run_id="run1", plan_path="plan.md", base_session_id="base-id")
    group = make_group()
    for role, generation in ((SessionRole.CODER, 1), (SessionRole.REVIEWER, 1)):
        record_session(
            manifest,
            group_id=group.id,
            group_name=group.name,
            summary=group.summary,
            entry=SessionEntry(
                session_id=str(uuid.uuid4()),
                role=role,
                generation=generation,
                name=session_display_name("run1", group.id, role.value, generation),
            ),
        )
    store.save(manifest)
    loaded = ManifestStore(paths).load()
    assert loaded.base_session_id == "base-id"
    entry = loaded.groups["g1"]
    assert entry.group_name == "fix auth flow" and entry.summary == group.summary
    roles = [(s.role, s.generation, s.name) for s in entry.sessions]
    assert roles == [
        (SessionRole.CODER, 1, "run1-g1-coder-g1"),
        (SessionRole.REVIEWER, 1, "run1-g1-reviewer-g1"),
    ]
    assert paths.manifest_path == tmp_path / ".orchestrator" / "runs" / "run1" / "manifest.json"


def test_group_artifacts_persist_under_the_run_dir_as_pointers(tmp_path):
    store = ManifestStore(RunPaths(tmp_path, "run1"))
    report = CoderReport(status="completed", summary="done")
    path = store.save_group_artifact("g1", artifact_name("report", 1, 2), report)
    assert (
        path == tmp_path / ".orchestrator" / "runs" / "run1" / "groups" / "g1" / "report-g1-r2.json"
    )
    assert CoderReport.model_validate_json(path.read_text()).summary == "done"


# ---------------------------------------------------------------- prompts (AE6)


def test_first_prompt_opens_with_a_parseable_identity_block():
    prompt = render_coder_prompt("run1", make_group())
    match = re.match(
        r'<run-manifest run_id="([^"]+)" group_id="([^"]+)" group_name="([^"]+)">\n'
        r"<summary>(.*?)</summary>\n</run-manifest>\n<spec>\n",
        prompt,
        re.DOTALL,
    )
    assert match, prompt[:200]
    assert match.groups()[:3] == ("run1", "g1", "fix auth flow")
    assert not prompt.startswith(INJECTED_PREFIXES)
    assert "<spec>" in prompt and "Refactor refresh()" in prompt
    assert "<run-report" in prompt  # report contract included
    assert "- [v1] unit tests pass" in prompt
    assert "- [v2] no new lint errors (optional)" in prompt


def test_identity_block_escapes_attribute_breaking_characters():
    group = make_group(name='auth "fast" <path>')
    prompt = render_coder_prompt("run1", group)
    assert 'group_name="auth &quot;fast&quot; &lt;path&gt;"' in prompt


def test_reviewer_prompt_ferries_pointers_not_payloads():
    prompt = render_reviewer_prompt(
        "run1", make_group(), report_path="/runs/r1/groups/g1/report-g1-r1.json", base_ref="abc123"
    )
    assert "/runs/r1/groups/g1/report-g1-r1.json" in prompt
    assert "git diff abc123" in prompt
    assert "approved | changes_required | too_hard" in prompt


def test_handoff_prompt_carries_generation_and_outstanding_items():
    prompt = render_handoff_prompt(
        "run1",
        make_group(),
        generation=2,
        retirement_reason="context tokens exceeded 120000",
        last_report='{"status": "blocked"}',
        outstanding="- fix the retry loop",
        diff_summary="2 files changed",
    )
    assert "generation 2" in prompt
    assert "context tokens exceeded 120000" in prompt
    assert "- fix the retry loop" in prompt
    assert prompt.startswith("<run-manifest")  # respawned sessions carry identity too


# ---------------------------------------------------------------- worktrees


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target-repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "test@test")
    git("config", "user.name", "test")
    (repo / "README.md").write_text("hello\n")
    git("add", ".")
    git("commit", "-m", "init")
    return repo


def test_worktree_path_keeps_repo_dir_name_as_substring(git_repo):
    path = worktree_path(git_repo, "g1", "Fix Auth Flow!")
    assert git_repo.name in str(path)  # analyzer allowlist substring rule
    assert path.name == "g1-fix-auth-flow"


def test_create_worktree_is_idempotent_and_checks_out_the_group_branch(git_repo):
    branch = group_branch("run1", "g1")
    path = create_worktree(
        git_repo, group_id="g1", name="fix auth", branch=branch, start_point="main"
    )
    again = create_worktree(
        git_repo, group_id="g1", name="fix auth", branch=branch, start_point="main"
    )
    assert path == again and (path / "README.md").is_file()
    head = subprocess.run(
        ["git", "branch", "--show-current"], cwd=path, capture_output=True, text=True
    ).stdout.strip()
    assert head == branch
    assert branch != integration_branch("run1")  # never nests under the integration ref


def test_create_worktree_resumes_an_existing_branch_after_removal(git_repo):
    branch = group_branch("run1", "g1")
    path = create_worktree(
        git_repo, group_id="g1", name="fix auth", branch=branch, start_point="main"
    )
    (path / "work.txt").write_text("progress\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "wip"], cwd=path, check=True, capture_output=True)
    remove_worktree(git_repo, path)
    resumed = create_worktree(
        git_repo, group_id="g1", name="fix auth", branch=branch, start_point="main"
    )
    assert (resumed / "work.txt").read_text() == "progress\n"


def test_remove_worktree_refuses_dirty_without_force_and_is_idempotent(git_repo):
    branch = group_branch("run1", "g1")
    path = create_worktree(
        git_repo, group_id="g1", name="fix auth", branch=branch, start_point="main"
    )
    (path / "uncommitted.txt").write_text("precious\n")
    with pytest.raises(WorktreeError, match="dirty"):
        remove_worktree(git_repo, path)
    remove_worktree(git_repo, path, force=True)
    assert not path.exists()
    remove_worktree(git_repo, path)  # already gone: no-op


def test_conflicting_directory_at_worktree_path_is_rejected(git_repo):
    path = worktree_path(git_repo, "g1", "fix auth")
    path.mkdir(parents=True)
    with pytest.raises(WorktreeError, match="not a worktree"):
        create_worktree(
            git_repo,
            group_id="g1",
            name="fix auth",
            branch=group_branch("r", "g1"),
            start_point="main",
        )
