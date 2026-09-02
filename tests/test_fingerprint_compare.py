"""U7: read the recorded index fingerprint back and compare it, on the
`run`/`resume` reuse path — never on a fresh `group` invocation.

Drives the real CLI (`main`) against the scripted claude stub, the same
harness `test_e2e_stub.py` uses, so a mismatch is exercised exactly where it
would show up for an operator: after `group` already wrote
`grouping-trace.json`'s `ProvenanceEntry.index_fingerprint`, but before `run`/
`resume` executes against groups.json.
"""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.cli import main
from orchestrator.grouping.graphing import CodegraphClient, index_fingerprint
from orchestrator.grouping.trace import GroupingTrace
from test_e2e_stub import (  # noqa: F401 -- fake_home, repo are pytest fixtures
    coder_entry,
    fake_home,
    name_of,
    repo,
    script_session,
    verdict_entry,
    write_config,
)
from test_grouper_pipeline import StubLlm, codegraph_response


def _drifted_response(args):
    """Same canned output as `codegraph_response`, except the one bulk
    `query ""` call `logical_export` issues — the call the fingerprint is
    computed from — returns a symbol that was never there before, so its
    fingerprint differs from `codegraph_response`'s own."""
    if args[0] == "query" and args[1] == "":
        return json.dumps(
            [{"node": {"id": "function:new_symbol", "kind": "function", "filePath": "new.py"}}]
        )
    return codegraph_response(args)


def _fingerprint_of(runner) -> str:
    client = CodegraphClient(repo_root=Path("."), runner=runner)
    return index_fingerprint(client.logical_export())


def _group(target_repo: Path, name: str = "plan") -> str:
    """Runs the real `group` command and returns the single produced group id."""
    exit_code = main(
        ["group", str(target_repo / "plan.md"), "--repo", str(target_repo), "--name", name],
        llm_runner=StubLlm(),
        client=CodegraphClient(repo_root=target_repo, runner=codegraph_response),
    )
    assert exit_code == 0
    groups_path = target_repo / ".orchestrator" / "groupings" / name / "groups.json"
    grouping = json.loads(groups_path.read_text())
    assert len(grouping["groups"]) == 1
    return grouping["groups"][0]["id"]


def _recorded_fingerprint(target_repo: Path, name: str = "plan") -> str:
    trace_path = target_repo / ".orchestrator" / "groupings" / name / "grouping-trace.json"
    trace = GroupingTrace.model_validate_json(trace_path.read_text())
    assert trace.provenance is not None
    return trace.provenance.index_fingerprint


class TestResumeWarnsOnDrift:
    def test_resume_with_a_different_index_warns_naming_both_and_executes_the_snapshot(
        self,
        repo,  # noqa: F811 -- pytest fixture imported from test_e2e_stub
        fake_home,  # noqa: F811 -- pytest fixture imported from test_e2e_stub
        capsys,
    ):
        """A run's grouping is frozen once groups executed against it, and the
        index drifts *because* the run merges work — so a resume must warn and
        carry on with its snapshot, never refuse (r20260902-132128 was stranded
        by the old hard failure after three merges)."""
        gid = _group(repo)
        recorded_fp = _recorded_fingerprint(repo)
        write_config(repo, fake_home)

        run_id = "r-mismatch"
        script_session(
            fake_home, name_of(run_id, gid, "coder"), {"exit_code": 1, "stderr": "worker crashed"}
        )
        exit_code = main(
            ["run", "--repo", str(repo), "--run-id", run_id],
            llm_runner=StubLlm(),
            client=CodegraphClient(repo_root=repo, runner=codegraph_response),
        )
        assert exit_code == 2  # interrupted, not the drift under test yet
        capsys.readouterr()

        stub = StubLlm()
        script_session(
            fake_home,
            name_of(run_id, gid, "coder"),
            coder_entry(files={f"{gid}.out": "done\n"}, commit=f"{gid}: work"),
        )
        script_session(fake_home, name_of(run_id, gid, "reviewer"), verdict_entry("approved"))
        exit_code = main(
            ["resume", run_id, "--repo", str(repo)],
            llm_runner=stub,
            client=CodegraphClient(repo_root=repo, runner=_drifted_response),
        )
        assert exit_code == 0
        err = capsys.readouterr().err
        assert "warning: codegraph index drifted" in err
        assert recorded_fp in err
        assert _fingerprint_of(_drifted_response) in err
        # no re-partition: the mapper was never invoked and the snapshot is intact
        assert not any(title == "mapper_output" for title, _ in stub.prompts)
        trace = GroupingTrace.model_validate_json(
            (repo / ".orchestrator" / "runs" / run_id / "grouping-trace.json").read_text()
        )
        assert trace.provenance is not None
        assert trace.provenance.index_fingerprint == recorded_fp


class TestAllowDriftNeverRepartitionsAResume:
    def test_allow_index_drift_on_resume_is_inert(
        self,
        repo,  # noqa: F811 -- pytest fixture imported from test_e2e_stub
        fake_home,  # noqa: F811 -- pytest fixture imported from test_e2e_stub
        capsys,
    ):
        gid = _group(repo)
        recorded_fp = _recorded_fingerprint(repo)
        write_config(repo, fake_home)

        run_id = "r-drift"
        script_session(
            fake_home, name_of(run_id, gid, "coder"), {"exit_code": 1, "stderr": "worker crashed"}
        )
        exit_code = main(
            ["run", "--repo", str(repo), "--run-id", run_id],
            llm_runner=StubLlm(),
            client=CodegraphClient(repo_root=repo, runner=codegraph_response),
        )
        assert exit_code == 2
        capsys.readouterr()

        stub = StubLlm()
        script_session(
            fake_home,
            name_of(run_id, gid, "coder"),
            coder_entry(files={f"{gid}.out": "done\n"}, commit=f"{gid}: work"),
        )
        script_session(fake_home, name_of(run_id, gid, "reviewer"), verdict_entry("approved"))
        exit_code = main(
            ["resume", run_id, "--repo", str(repo), "--allow-index-drift"],
            llm_runner=stub,
            client=CodegraphClient(repo_root=repo, runner=_drifted_response),
        )
        assert exit_code == 0
        err = capsys.readouterr().err
        assert "never re-partitions a resume" in err
        # the flag re-partitions a `run`; on `resume` the half-executed DAG is
        # the only DAG — the mapper stays silent and the snapshot keeps its
        # recorded fingerprint
        assert not any(title == "mapper_output" for title, _ in stub.prompts)
        assert not any(title == "speccer_output" for title, _ in stub.prompts)
        new_trace = GroupingTrace.model_validate_json(
            (repo / ".orchestrator" / "runs" / run_id / "grouping-trace.json").read_text()
        )
        assert new_trace.provenance is not None
        assert new_trace.provenance.index_fingerprint == recorded_fp


class TestFreshGroupUnaffected:
    def test_fresh_group_invocation_never_fails_on_mismatch(
        self,
        repo,  # noqa: F811 -- pytest fixture imported from test_e2e_stub
    ):
        _group(repo, name="alpha")
        recorded_fp = _recorded_fingerprint(repo, name="alpha")

        # re-grouping the very same name against a drifted index is a *fresh*
        # `group` invocation — it must succeed and simply record the new
        # fingerprint, never compare against the one already on disk.
        exit_code = main(
            ["group", str(repo / "plan.md"), "--repo", str(repo), "--name", "alpha"],
            llm_runner=StubLlm(),
            client=CodegraphClient(repo_root=repo, runner=_drifted_response),
        )
        assert exit_code == 0
        new_fp = _recorded_fingerprint(repo, name="alpha")
        assert new_fp != recorded_fp


class TestMatchProceedsSilently:
    def test_resume_with_a_matching_index_prints_no_drift_warning(
        self,
        repo,  # noqa: F811 -- pytest fixture imported from test_e2e_stub
        fake_home,  # noqa: F811 -- pytest fixture imported from test_e2e_stub
        capsys,
    ):
        gid = _group(repo)
        write_config(repo, fake_home)

        run_id = "r-match"
        script_session(
            fake_home, name_of(run_id, gid, "coder"), {"exit_code": 1, "stderr": "worker crashed"}
        )
        exit_code = main(
            ["run", "--repo", str(repo), "--run-id", run_id],
            llm_runner=StubLlm(),
            client=CodegraphClient(repo_root=repo, runner=codegraph_response),
        )
        assert exit_code == 2
        capsys.readouterr()

        script_session(
            fake_home,
            name_of(run_id, gid, "coder"),
            coder_entry(files={f"{gid}.out": "done\n"}, commit=f"{gid}: work"),
        )
        script_session(fake_home, name_of(run_id, gid, "reviewer"), verdict_entry("approved"))
        exit_code = main(
            ["resume", run_id, "--repo", str(repo)],
            llm_runner=StubLlm(),
            # same canned responses `_group` used above: the current index's
            # fingerprint matches the recorded one exactly.
            client=CodegraphClient(repo_root=repo, runner=codegraph_response),
        )
        assert exit_code == 0
        out_err = capsys.readouterr()
        assert "index drift" not in out_err.err
        assert "fingerprint mismatch" not in out_err.err
