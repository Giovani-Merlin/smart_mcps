"""U6: the four real g5/g7-class failures, reproduced against the zero-token
stub harness (tests/fake_claude.py) — at zero token cost, no network, no real
``claude`` binary. Each scenario fails without its fix and passes with it;
where reverting the fix in-tree is impractical, the assertion is specific
enough that its failure mode is unambiguous. Escalation defaults to enabled
(plan U2), so every scenario below sets HITL explicitly (auto-answer via
``_drive_escalations`` or ``--intensity autonomous``) rather than blocking.

Reuses the E2E harness fixtures and helpers from test_e2e_stub.py (repo,
fake_home, write_config, script_session, coder_entry, verdict_entry,
name_of, state_of, manifest_of, calls_of, git) rather than reinventing them.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time


from orchestrator.cli import main
from orchestrator.execution.escalation import pending_escalations
from orchestrator.execution.manifest import RunPaths, atomic_write_text
from orchestrator.model import EscalationResponse, HumanAction
from test_cli import make_group, write_run_artifacts
from test_e2e_stub import (  # noqa: F401 -- fake_home, repo are pytest fixtures
    StubLlm,
    fake_home,
    git,
    manifest_of,
    name_of,
    repo,
    script_session,
    state_of,
    verdict_entry,
    write_config,
)


def coder_report_entry(status: str, **body_extra) -> dict:
    """A raw scripted coder report, for statuses test_e2e_stub's ``coder_entry``
    doesn't parametrize (here: ``permission_denied``'s ``denied_command``)."""
    body: dict = {
        "status": status,
        "summary": "scripted round",
        "verification_results": [],
        "surprises": [],
        **body_extra,
    }
    return {"result": f'<run-report status="{status}">\n{json.dumps(body)}\n</run-report>'}


def _drive_escalations(paths: RunPaths, thread: threading.Thread, plan: dict) -> list[str]:
    handled: list[str] = []
    seen: set[str] = set()
    deadline = time.monotonic() + 20.0
    while (thread.is_alive() or pending_escalations(paths)) and time.monotonic() < deadline:
        for request in pending_escalations(paths):
            if request.id in seen:
                continue
            action, text = plan.get(request.kind.value, ("answer", ""))
            atomic_write_text(
                paths.escalations_dir / f"response-{request.id}.json",
                EscalationResponse(
                    id=request.id, action=HumanAction(action), answer=text
                ).model_dump_json(),
            )
            seen.add(request.id)
            handled.append(request.kind.value)
        time.sleep(0.02)
    return handled


# ------------------------------------------------------------- empty branch


def test_fault_empty_branch_is_refused_and_never_completes(repo, fake_home, capsys):  # noqa: F811 -- pytest fixtures imported from test_e2e_stub
    """A scripted coder that writes files but never commits produces a branch
    with zero commits ahead of the integration tip. IntegrationMerger.merge_group
    refuses that direct merge attempt (plan U1) — the group can never reach
    ``completed`` through the review loop's own merge. Because U2's autonomous
    resolve is wired by default, the orchestrator (not the worker) then commits
    the stranded uncommitted work itself and merges it — landing ``resolved``,
    a state the plan explicitly keeps distinct from ``completed`` precisely
    because it never had that direct, reviewed merge succeed."""
    run_id = "rf1"
    write_run_artifacts(repo, [make_group("g1", files=["g1.out"])])
    write_config(repo, fake_home)
    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        {
            "result": (
                '<run-report status="completed">\n'
                '{"status": "completed", "summary": "wrote it, forgot to commit", '
                '"verification_results": [], "surprises": []}\n'
                "</run-report>"
            ),
            "files": {"g1.out": "never committed\n"},
            # deliberately no "commit" key
        },
    )
    script_session(fake_home, name_of(run_id, "g1", "reviewer"), verdict_entry("approved"))

    exit_code = main(
        ["run", "--repo", str(repo), "--run-id", run_id, "--intensity", "autonomous"],
        llm_runner=StubLlm(),
    )
    assert exit_code == 1  # not every group completed
    state = state_of(repo, run_id)
    # Never completed through the direct merge path — U1's refusal is real, even
    # though U2's separate resolve mechanism goes on to rescue the stranded work.
    assert state["groups"]["g1"]["state"] != "completed"
    assert state["groups"]["g1"]["state"] == "resolved"

    out = capsys.readouterr().out
    assert "has no commits ahead of" in out and "refusing to merge nothing" in out

    # the resolve routine's own merge (a distinct operation from the review
    # loop's direct merge, which never ran to success) is what lands g1.
    log = git(repo, "log", "--oneline", f"orchestrator/run-{run_id}")
    assert f"merge({run_id}): g1" in log
    assert f"resolve({run_id}): g1" in log


# --------------------------------------------------------------- stale base


def test_fault_stale_base_resumed_group_absorbs_a_concurrent_sibling_merge(repo, fake_home):  # noqa: F811 -- pytest fixtures imported from test_e2e_stub
    """g1 and g2 start concurrently (their worktrees are cut from the same,
    pre-merge tip); g2 is interrupted on its very first round while g1 goes on
    to complete and merge. On `resume`, g2 re-enters its *existing* worktree —
    the path that, before plan U1, ignored the integration tip entirely — and
    must refresh onto it, so g1's commit ends up reachable from g2's own
    branch tip once g2 also lands."""
    run_id = "rf2"
    write_run_artifacts(
        repo,
        [
            make_group("g1", files=["g1.out"]),
            make_group("g2", files=["g2.out"]),
        ],
    )
    write_config(repo, fake_home)
    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        {"delay_s": 0.2, **_files_and_commit({"g1.out": "one\n"}, "g1: work")},
    )
    script_session(fake_home, name_of(run_id, "g1", "reviewer"), verdict_entry("approved"))
    # g2's reviewer only runs after the resume, but its script must exist up
    # front: fake_claude binds a session to scripts/<name>.jsonl at creation
    # time, and an unbound session falls back to the (empty) shared queue.
    script_session(fake_home, name_of(run_id, "g2", "reviewer"), verdict_entry("approved"))
    # g2's coder dies at fork on its first round — an envelope failure, not a
    # work failure, so the group lands interrupted (plan U1/R1-R3).
    script_session(
        fake_home, name_of(run_id, "g2", "coder"), {"exit_code": 1, "stderr": "worker crashed"}
    )

    exit_code = main(
        [
            "run",
            "--repo",
            str(repo),
            "--run-id",
            run_id,
            "--concurrency",
            "2",
            "--intensity",
            "autonomous",
        ],
        llm_runner=StubLlm(),
    )
    assert exit_code == 2  # stopped-but-resumable (an INTERRUPTED group, not FAILED)
    state = state_of(repo, run_id)
    assert state["groups"]["g1"]["state"] == "completed"
    assert state["groups"]["g2"]["state"] == "interrupted"
    g1_sha = git(repo, "rev-parse", f"orchestrator/run-{run_id}").strip()

    script_session(
        fake_home,
        name_of(run_id, "g2", "coder"),
        coder_entry_completed({"g2.out": "two\n"}, "g2: work"),
    )
    # `resume` restores the run's persisted escalation config (plan U2) — no
    # need to re-state --intensity autonomous here.
    exit_code = main(["resume", run_id, "--repo", str(repo)], llm_runner=StubLlm())
    assert exit_code == 0
    state = state_of(repo, run_id)
    assert state["groups"]["g2"]["state"] == "completed"

    log = git(repo, "log", "--oneline", f"orchestrator/run-{run_id}")
    assert f"merge({run_id}): g1" in log and f"merge({run_id}): g2" in log
    g2_merge_sha = next(
        line.split()[0] for line in log.splitlines() if f"merge({run_id}): g2" in line
    )
    g2_branch_tip = git(repo, "rev-parse", f"{g2_merge_sha}^2").strip()
    # g1's commit is reachable from g2's own branch tip — g2 absorbed the
    # sibling's merge before it landed its own work, not merely alongside it.
    result = subprocess.run(["git", "merge-base", "--is-ancestor", g1_sha, g2_branch_tip], cwd=repo)
    assert result.returncode == 0, "g1's commit is not reachable from g2's own branch tip"


def test_resume_restores_the_run_s_persisted_escalation_config_without_reflag(  # noqa: F811 -- pytest fixtures imported from test_e2e_stub
    repo, fake_home, capsys
):
    """plan U2 regression: `run --intensity autonomous` persists that tier onto
    the manifest; a bare `resume` (no escalation flags at all) must restore it
    rather than reverting to EscalationConfig()'s on_stuck/HITL-on default —
    verified via the persisted manifest and the banner's HITL line, never by
    re-triggering an actual escalation block."""
    run_id = "rf-esc"
    write_run_artifacts(repo, [make_group("g1", files=["g1.out"])])
    write_config(repo, fake_home)
    # g1's coder dies at fork on its first round, so the run stops resumable
    # (INTERRUPTED) without ever finishing — resume is required to complete it.
    script_session(
        fake_home, name_of(run_id, "g1", "coder"), {"exit_code": 1, "stderr": "worker crashed"}
    )

    exit_code = main(
        ["run", "--repo", str(repo), "--run-id", run_id, "--intensity", "autonomous"],
        llm_runner=StubLlm(),
    )
    assert exit_code == 2
    manifest = manifest_of(repo, run_id)
    assert manifest["escalation"]["intensity"] == "autonomous"
    capsys.readouterr()

    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        coder_entry_completed({"g1.out": "one\n"}, "g1: work"),
    )
    script_session(fake_home, name_of(run_id, "g1", "reviewer"), verdict_entry("approved"))
    exit_code = main(["resume", run_id, "--repo", str(repo)], llm_runner=StubLlm())
    assert exit_code == 0
    out = capsys.readouterr().out
    banner = next(line for line in out.splitlines() if line.startswith("run "))
    assert "HITL off" in banner
    manifest = manifest_of(repo, run_id)
    assert manifest["escalation"]["intensity"] == "autonomous"


def test_resume_explicit_intensity_flag_overrides_the_persisted_value(  # noqa: F811 -- pytest fixtures imported from test_e2e_stub
    repo, fake_home, capsys
):
    """An explicit --intensity on resume still wins over the persisted value,
    matching today's flag > config-file precedence."""
    run_id = "rf-esc2"
    write_run_artifacts(repo, [make_group("g1", files=["g1.out"])])
    write_config(repo, fake_home)
    script_session(
        fake_home, name_of(run_id, "g1", "coder"), {"exit_code": 1, "stderr": "worker crashed"}
    )
    exit_code = main(
        ["run", "--repo", str(repo), "--run-id", run_id, "--intensity", "autonomous"],
        llm_runner=StubLlm(),
    )
    assert exit_code == 2
    capsys.readouterr()

    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        coder_entry_completed({"g1.out": "one\n"}, "g1: work"),
    )
    script_session(fake_home, name_of(run_id, "g1", "reviewer"), verdict_entry("approved"))
    exit_code = main(
        ["resume", run_id, "--repo", str(repo), "--intensity", "on_stuck"], llm_runner=StubLlm()
    )
    out = capsys.readouterr().out
    banner = next(line for line in out.splitlines() if line.startswith("run "))
    assert "HITL on (intensity=on_stuck" in banner
    assert exit_code == 0


def _files_and_commit(files: dict, commit: str) -> dict:
    body = {
        "status": "completed",
        "summary": "scripted round",
        "verification_results": [],
        "surprises": [],
    }
    return {
        "result": f'<run-report status="completed">\n{json.dumps(body)}\n</run-report>',
        "files": files,
        "commit": commit,
    }


def coder_entry_completed(files: dict, commit: str) -> dict:
    return _files_and_commit(files, commit)


# ------------------------------------------------------------- overlap gate


def test_fault_overlap_gate_escalates_with_hitl_on(repo, fake_home):  # noqa: F811 -- pytest fixtures imported from test_e2e_stub
    """g1 fails (reject-forever) and declares a file g2 also declares, with no
    DAG dependency between them — plan U2/U9's file-overlap hold. With HITL on,
    the failure raises a group_resolve escalation naming g1, g2, and the
    shared file; the run does not silently let g2 through unremarked."""
    run_id = "rf3"
    write_run_artifacts(
        repo,
        [
            make_group("g1", files=["shared.py"]),
            make_group("g2", files=["shared.py", "g2.out"]),
        ],
    )
    write_config(
        repo,
        fake_home,
        "[breaker]\nmax_rounds_per_generation = 1\nmax_generations = 1\n"
        "[escalation]\npoll_interval_s = 0.02\n",
    )
    script_session(fake_home, name_of(run_id, "g1", "coder"), {"result": _completed_report()})
    script_session(
        fake_home,
        name_of(run_id, "g1", "reviewer"),
        verdict_entry("changes_required", ["never good enough"]),
    )
    script_session(
        fake_home,
        name_of(run_id, "g2", "coder"),
        coder_entry_completed({"g2.out": "done\n"}, "g2: work"),
    )
    script_session(fake_home, name_of(run_id, "g2", "reviewer"), verdict_entry("approved"))

    paths = RunPaths(repo, run_id)
    outcome: dict = {}

    def run() -> None:
        outcome["code"] = main(
            ["run", "--repo", str(repo), "--run-id", run_id, "--hitl", "--sequential"],
            llm_runner=StubLlm(),
        )

    thread = threading.Thread(target=run)
    thread.start()
    handled = _drive_escalations(paths, thread, {"group_resolve": ("answer", "release it")})
    thread.join(timeout=25)
    assert not thread.is_alive()

    assert "group_resolve" in handled
    state = state_of(repo, run_id)
    assert state["groups"]["g1"]["state"] in ("failed", "resolved")
    assert state["groups"]["g2"]["state"] == "completed"

    esc_dir = repo / ".orchestrator" / "runs" / run_id / "escalations"
    requests = [json.loads(p.read_text()) for p in esc_dir.glob("request-*.json")]
    resolve_reqs = [r for r in requests if r["kind"] == "group_resolve"]
    assert resolve_reqs
    prompt = resolve_reqs[0]["prompt"]
    assert "g1" in prompt and "g2" in prompt and "shared.py" in prompt


def test_fault_overlap_gate_holds_then_releases_with_hitl_off(repo, fake_home):  # noqa: F811 -- pytest fixtures imported from test_e2e_stub
    """Same shape, HITL off (`--intensity autonomous`): no escalation is ever
    raised, the failed group's resolve runs headless, and g2 — held while g1
    was unresolved — still reaches the integration branch once g1 settles."""
    run_id = "rf4"
    write_run_artifacts(
        repo,
        [
            make_group("g1", files=["shared.py"]),
            make_group("g2", files=["shared.py", "g2.out"]),
        ],
    )
    write_config(repo, fake_home, "[breaker]\nmax_rounds_per_generation = 1\nmax_generations = 1\n")
    script_session(fake_home, name_of(run_id, "g1", "coder"), {"result": _completed_report()})
    script_session(
        fake_home,
        name_of(run_id, "g1", "reviewer"),
        verdict_entry("changes_required", ["never good enough"]),
    )
    script_session(
        fake_home,
        name_of(run_id, "g2", "coder"),
        coder_entry_completed({"g2.out": "done\n"}, "g2: work"),
    )
    script_session(fake_home, name_of(run_id, "g2", "reviewer"), verdict_entry("approved"))

    exit_code = main(
        [
            "run",
            "--repo",
            str(repo),
            "--run-id",
            run_id,
            "--sequential",
            "--intensity",
            "autonomous",
        ],
        llm_runner=StubLlm(),
    )
    state = state_of(repo, run_id)
    assert state["groups"]["g1"]["state"] in ("failed", "resolved")
    assert state["groups"]["g2"]["state"] == "completed"
    assert exit_code in (0, 1)

    esc_dir = repo / ".orchestrator" / "runs" / run_id / "escalations"
    assert not esc_dir.exists() or not list(esc_dir.glob("request-*.json"))

    log = git(repo, "log", "--oneline", f"orchestrator/run-{run_id}")
    assert f"merge({run_id}): g2" in log


def _completed_report() -> str:
    body = {
        "status": "completed",
        "summary": "scripted round",
        "verification_results": [],
        "surprises": [],
    }
    return f'<run-report status="completed">\n{json.dumps(body)}\n</run-report>'


# --------------------------------------------------------------- typed denial


def test_fault_typed_denial_interrupts_resumes_and_costs_no_rewrite(repo, fake_home):  # noqa: F811 -- pytest fixtures imported from test_e2e_stub
    """A scripted coder reporting permission_denied leaves the group
    interrupted (never failed), resumable by a plain `resume`, and its
    rewrite budget untouched — no extra generation is spent on the denial."""
    run_id = "rf5"
    write_run_artifacts(repo, [make_group("g1", files=["g1.out"])])
    write_config(repo, fake_home)
    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        coder_report_entry("permission_denied", denied_command="rm -rf /some/protected/path"),
    )

    exit_code = main(
        ["run", "--repo", str(repo), "--run-id", run_id, "--intensity", "autonomous"],
        llm_runner=StubLlm(),
    )
    assert exit_code == 2  # stopped-but-resumable, never failed
    state = state_of(repo, run_id)
    assert state["groups"]["g1"]["state"] == "interrupted"
    assert "rm -rf /some/protected/path" in state["groups"]["g1"]["failure"]

    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        coder_entry_completed({"g1.out": "done\n"}, "g1: work"),
    )
    script_session(fake_home, name_of(run_id, "g1", "reviewer"), verdict_entry("approved"))
    exit_code = main(["resume", run_id, "--repo", str(repo)], llm_runner=StubLlm())
    assert exit_code == 0
    state = state_of(repo, run_id)
    assert state["groups"]["g1"]["state"] == "completed"
    assert state["groups"]["g1"]["generation"] == 1  # no rewrite spent on the denial

    manifest = manifest_of(repo, run_id)
    coder_sessions = [s for s in manifest["groups"]["g1"]["sessions"] if s["role"] == "coder"]
    assert len(coder_sessions) == 1  # resumed the same session, no second fork
