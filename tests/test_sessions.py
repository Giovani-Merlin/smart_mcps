"""U5 tests: session runner, report rounds, manifest, worktrees (plan Phase B).

Every test runs against tests/fake_claude.py — zero live CLI calls, zero tokens
(plan R24). The stub speaks the envelope shapes the U5 spike verified.
"""

from __future__ import annotations

import os
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
    RoundUsage,
    SessionError,
    SessionRunner,
    UsageLimit,
    is_usage_limit,
    nudge_until_report,
    parse_report,
    session_display_name,
)
from orchestrator.execution.worktrees import (
    DENIED_GIT_SUBCOMMANDS,
    WorktreeError,
    WorktreeRefreshConflict,
    create_worktree,
    denied_git_tool_patterns,
    group_branch,
    integration_branch,
    is_denied_git_invocation,
    provision_env,
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


def test_thinking_budget_is_pinned_on_every_worker_call(fake_home, tmp_path):
    """Left unpinned the CLI picks its own thinking level, which is invisible in every
    run artifact and lands on output tokens — the measured cost driver (run
    r20260729-correctness: 588k output tokens in one round)."""
    runner = make_runner(fake_home, max_thinking_tokens=4000, thinking="adaptive")
    runner.start_base(run_id="run1", base_context="ctx", cwd=tmp_path)
    (call,) = calls(fake_home)
    argv = call["argv"]
    assert argv[argv.index("--max-thinking-tokens") + 1] == "4000"
    assert argv[argv.index("--thinking") + 1] == "adaptive"


def test_thinking_flags_are_omitted_when_unset(fake_home, tmp_path):
    runner = make_runner(fake_home)
    runner.start_base(run_id="run1", base_context="ctx", cwd=tmp_path)
    (call,) = calls(fake_home)
    assert "--max-thinking-tokens" not in call["argv"]
    assert "--thinking" not in call["argv"]


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


def test_cumulative_usage_keeps_the_token_classes_apart(fake_home, tmp_path):
    """A run whose spend is mostly cache reads is cheap; one that is mostly
    uncached input is not. Folding them into a single input total (as this did
    originally) makes the two indistinguishable in an estimate-vs-actual view."""
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
    assert usage.total_input_tokens == 12  # uncached input only, not 12 + cache_creation
    assert usage.total_cache_read_tokens == 3000
    assert usage.total_cache_creation_tokens == 400


def test_multi_turn_envelope_reports_last_turn_context_not_the_round_sum():
    """A real multi-turn envelope sums every turn into the top-level ``usage``, so
    reading it as context occupancy inflates without bound: the live run saw a
    190-turn coder round report 18,606,845 against a real occupancy of ~262k, which
    tripped the 120k breaker on every group that needed a second round. Shape taken
    from CLI 2.1.211's `--output-format json`.
    """
    envelope = {
        "usage": {
            # top level = the sum across both turns, which is NOT the context size
            "input_tokens": 4,
            "output_tokens": 300,
            "cache_read_input_tokens": 261_000,
            "cache_creation_input_tokens": 500,
            "iterations": [
                {
                    "input_tokens": 2,
                    "output_tokens": 35,
                    "cache_read_input_tokens": 100_000,
                    "cache_creation_input_tokens": 282,
                },
                {
                    "input_tokens": 2,
                    "output_tokens": 265,
                    "cache_read_input_tokens": 161_000,
                    "cache_creation_input_tokens": 218,
                },
            ],
        }
    }
    usage = RoundUsage.from_envelope(envelope)
    assert usage.context_tokens == 2 + 265 + 161_000 + 218
    assert usage.context_tokens < 261_804  # the sum, had we read the top level


def test_envelope_without_iterations_falls_back_to_top_level_usage():
    """Older CLIs and the test stub emit no ``iterations``; for a single turn the
    top-level totals *are* the last turn, so the fallback stays exact."""
    envelope = {
        "usage": {
            "input_tokens": 2,
            "output_tokens": 4,
            "cache_read_input_tokens": 7_370,
            "cache_creation_input_tokens": 17_158,
        }
    }
    assert RoundUsage.from_envelope(envelope).context_tokens == 2 + 4 + 7_370 + 17_158


def test_long_round_completes_with_no_per_round_timeout(fake_home, tmp_path):
    """R7: rounds run as long as the CLI does. The scripted delay outlasts the
    0.5s timeout the deleted RoundTimeout test used to kill this exact round at
    (tests never scripted a longer one); the round now completes normally."""
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    script(fake_home, {"delay_s": 1.5, "result": "slow but fine"})
    result = runner.resume(session_id=base.session_id, prompt="slow", cwd=tmp_path)
    assert result.text == "slow but fine"


def test_worker_env_scrubs_the_orchestrators_virtualenv(fake_home, tmp_path, monkeypatch):
    """U6/R16: workers must resolve the worktree's venv, not inherit the
    orchestrator's — VIRTUAL_ENV and every PATH entry under it are dropped."""
    venv = str(tmp_path / "orch-venv")
    monkeypatch.setenv("VIRTUAL_ENV", venv)
    monkeypatch.setenv("PATH", f"{venv}/bin:/usr/bin:/bin")
    runner = make_runner(fake_home)
    runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    (call,) = calls(fake_home)
    assert "VIRTUAL_ENV" not in call["env"]
    entries = call["env"]["PATH"].split(":")
    assert all(not entry.startswith(venv) for entry in entries)
    assert "/usr/bin" in entries and "/bin" in entries  # the rest of PATH survives


def test_worker_env_without_virtualenv_passes_through(fake_home, tmp_path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    runner = make_runner(fake_home)
    runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    (call,) = calls(fake_home)
    assert call["env"]["PATH"] == "/usr/bin:/bin"


def test_scripted_cli_failure_surfaces_stderr(fake_home, tmp_path):
    runner = make_runner(fake_home)
    script(fake_home, {"exit_code": 2, "stderr": "boom"})
    with pytest.raises(SessionError, match="boom"):
        runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)


def test_usage_limit_style_failure_surfaces_stdout_result_over_empty_stderr(fake_home, tmp_path):
    """Plan U4: a usage-limit exit has empty stderr but a populated JSON
    envelope on stdout — the error message must carry that text, not be empty."""
    runner = make_runner(fake_home)
    script(
        fake_home,
        {
            "exit_code": 1,
            "stderr": "",
            "stdout": json.dumps({"result": "Claude AI usage limit reached|1700000000"}),
        },
    )
    # Typed, not merely worded (plan P6): the re-entry path forks a fresh
    # generation on a plain SessionError, which against a usage limit fails
    # identically and burns a generation. `UsageLimit` is still a `SessionError`,
    # so the scheduler keeps classifying it INTERRUPTED / resumable.
    with pytest.raises(UsageLimit, match="Claude AI usage limit reached") as caught:
        runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    assert isinstance(caught.value, SessionError)


def test_a_broken_call_is_not_mistaken_for_a_usage_limit(fake_home, tmp_path):
    """The other side of the classification: an ordinary crash must stay a plain
    SessionError, or every failed session would stop being able to fall back to a
    fresh fork."""
    runner = make_runner(fake_home)
    script(fake_home, {"exit_code": 1, "stderr": "Segmentation fault", "stdout": ""})
    with pytest.raises(SessionError) as caught:
        runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    assert not isinstance(caught.value, UsageLimit)


def test_is_usage_limit_recognizes_the_wordings_seen_in_the_wild():
    """Every string here is one that was actually observed, not one that seemed
    plausible. The wordings are undocumented and differ by limit type, so the
    only safe way to extend this is to add evidence.

    The second was caught by the live tier on 2026-08-13, and it is the reason
    this test is worth having: the original pattern set was written around
    `usage limit reached|<epoch>` and did **not** match the sentence a real
    session limit produces — which would have sent a limited run straight down
    the pointless-fork path P6 exists to prevent.
    """
    assert is_usage_limit("Claude AI usage limit reached|1700000000")
    assert is_usage_limit("You've hit your session limit · resets 1pm (Europe/Berlin)")
    assert is_usage_limit("rate limited")
    assert is_usage_limit("429 Too Many Requests")
    assert not is_usage_limit("claude exited 1: Segmentation fault")
    assert not is_usage_limit("")
    # Not every sentence with "limit" in it is one: a model reporting that it hit
    # a *code* limit must still fall back to a fresh fork.
    assert not is_usage_limit("recursion limit exceeded in the parser")


def test_failure_with_unparseable_stdout_falls_back_to_stderr_unchanged(fake_home, tmp_path):
    runner = make_runner(fake_home)
    script(fake_home, {"exit_code": 1, "stderr": "", "stdout": "not json at all"})
    with pytest.raises(SessionError) as excinfo:
        runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    assert "not json at all" not in str(excinfo.value)
    assert str(excinfo.value).endswith(": ")


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
    # U6/R17: the dependency workflow is stated where the worker reads it
    assert "`uv sync`" in prompt and "inside the worktree" in prompt
    assert "imports a new" in prompt and "must pass here" in prompt


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
    # U6/R17: the dependency workflow rides the handoff too
    assert "`uv sync`" in prompt and "inside the worktree" in prompt
    assert "imports a new dependency" in prompt and "must pass here" in prompt


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


def test_provision_env_runs_uv_sync_only_in_uv_worktrees(tmp_path):
    recorded: list[tuple[list[str], Path]] = []

    def fake_run(argv, **kwargs):
        recorded.append((argv, kwargs.get("cwd")))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    bare = tmp_path / "bare"
    bare.mkdir()
    assert provision_env(bare, runner=fake_run) is False
    assert recorded == []  # no pyproject.toml / uv.lock: skipped silently

    marked = tmp_path / "marked"
    marked.mkdir()
    (marked / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert provision_env(marked, runner=fake_run) is True
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "uv.lock").write_text("")
    assert provision_env(locked, runner=fake_run) is True
    assert recorded == [(["uv", "sync"], marked), (["uv", "sync"], locked)]


def test_provision_env_passes_the_cache_env_and_the_configured_extras(tmp_path):
    """P7: the sync must warm the *worker's* cache, and build the *dev* env.

    It ran with no `env=` at all, so it warmed the operator's `~/.cache/uv` while
    every worker it provisioned for used the orchestrator's cache root — two
    caches, the worker's cold on a venv the other had already built, plus the
    cross-filesystem rename that produced the observed EXDEV.

    `--all-extras` is the second half: a group's venv should mirror the dev
    environment its work is verified against, or its reviewer cannot tell a
    missing extra from a regression.
    """
    recorded: list[dict] = []

    def fake_run(argv, **kwargs):
        recorded.append({"argv": argv, "env": kwargs.get("env")})
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "uv.lock").write_text("")
    assert (
        provision_env(
            worktree,
            runner=fake_run,
            env={"UV_CACHE_DIR": "/caches/uv"},
            extra_args=["--all-extras"],
        )
        is True
    )
    assert recorded[0]["argv"] == ["uv", "sync", "--all-extras"]
    env = recorded[0]["env"]
    assert env["UV_CACHE_DIR"] == "/caches/uv"
    assert env["PATH"] == os.environ["PATH"]  # overlaid on the real environment

    # No env given → no env passed, i.e. plain inheritance (the old behaviour,
    # still what a test or a non-cache caller gets).
    provision_env(worktree, runner=fake_run)
    assert recorded[1]["env"] is None
    assert recorded[1]["argv"] == ["uv", "sync"]


def test_provision_env_failure_logs_an_event_and_does_not_raise(tmp_path, capsys):
    """A fixable env hiccup must never kill the group: warn + log, no raise."""
    events: list[str] = []

    def failing_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="resolution failed")

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "uv.lock").write_text("")
    assert provision_env(worktree, runner=failing_run, log=events.append) is False
    assert len(events) == 1
    assert "uv sync failed" in events[0] and "resolution failed" in events[0]
    assert "uv sync failed" in capsys.readouterr().err

    def missing_uv(argv, **kwargs):
        raise OSError("No such file or directory: 'uv'")

    assert provision_env(worktree, runner=missing_uv, log=events.append) is False
    assert len(events) == 2  # OSError (uv absent) rides the same non-fatal path


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


def _git_config(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "config", *args], cwd=cwd, capture_output=True, text=True
    ).stdout.strip()


def test_worktree_reports_worktree_config_extension_and_isolates_user_email(git_repo):
    branch = group_branch("run1", "g1")
    path = create_worktree(
        git_repo, group_id="g1", name="fix auth", branch=branch, start_point="main"
    )
    assert _git_config(path, "extensions.worktreeConfig") == "true"
    subprocess.run(
        ["git", "config", "--worktree", "user.email", "worker@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    assert _git_config(path, "user.email") == "worker@example.com"
    assert _git_config(git_repo, "user.email") == "test@test"


def test_create_worktree_idempotent_and_rejects_other_branch(git_repo):
    branch = group_branch("run1", "g1")
    path = create_worktree(
        git_repo, group_id="g1", name="fix auth", branch=branch, start_point="main"
    )
    again = create_worktree(
        git_repo, group_id="g1", name="fix auth", branch=branch, start_point="main"
    )
    assert path == again

    subprocess.run(
        ["git", "checkout", "-b", "other"], cwd=git_repo, check=True, capture_output=True
    )
    other_path = worktree_path(git_repo, "g2", "fix auth 2")
    subprocess.run(
        ["git", "worktree", "add", str(other_path), "-b", "some-other-branch"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    with pytest.raises(WorktreeError, match="some-other-branch"):
        create_worktree(
            git_repo,
            group_id="g2",
            name="fix auth 2",
            branch=group_branch("run1", "g2"),
            start_point="main",
        )


def test_denied_git_subcommands_are_rejected_others_accepted():
    for denied in DENIED_GIT_SUBCOMMANDS:
        assert is_denied_git_invocation(list(denied))
    assert is_denied_git_invocation(["worktree", "prune"])
    for allowed in (["status"], ["add", "-A"], ["commit", "-m", "x"], ["diff"]):
        assert not is_denied_git_invocation(allowed)
    patterns = denied_git_tool_patterns()
    assert "Bash(git stash:*)" in patterns
    assert "Bash(git reset --hard:*)" in patterns
    assert "Bash(git worktree prune:*)" in patterns


def test_refresh_conflict_raises_worktree_refresh_conflict_names_paths_and_aborts(git_repo):
    branch = group_branch("run1", "g1")
    path = create_worktree(
        git_repo, group_id="g1", name="fix auth", branch=branch, start_point="main"
    )
    (path / "README.md").write_text("worker change\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "worker edit"], cwd=path, check=True, capture_output=True
    )

    (git_repo / "README.md").write_text("operator change\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "operator edit"], cwd=git_repo, check=True, capture_output=True
    )

    with pytest.raises(WorktreeRefreshConflict, match="README.md"):
        create_worktree(git_repo, group_id="g1", name="fix auth", branch=branch, start_point="main")
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True
    ).stdout
    assert "UU" not in status
    assert not (path / ".git").is_dir()  # linked worktree: .git is a file, not a dir
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "MERGE_HEAD"], cwd=path, capture_output=True
    )
    assert result.returncode != 0  # merge was aborted, no dangling MERGE_HEAD
