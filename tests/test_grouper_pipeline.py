"""Tests for the U4 grouper pipeline — LLM at the edges, deterministic core.

Written against the pipeline seams (plan U4 execution note): both LLM calls are
stubbed through the runner seam, codegraph through the client runner seam, and
determinism is asserted on the serialized output.
"""

import json

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.grouping.base_context import compile_base_context
from orchestrator.grouping.graphing import CodegraphClient
from orchestrator.grouping.estimator import estimate_group_tokens
from orchestrator.grouping.llm import LlmError, call_llm_json
from orchestrator.grouping.mapper import MapperOutput
from orchestrator.grouping.pipeline import GrouperError, run_grouping, serialize_grouping

PLAN_TEXT = """# feat: toy plan

## Tasks

- T1: extend the proxy server tool list
- T2: cover the proxy with tests
"""


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "CLAUDE.md").write_text("# Conventions\n\nUse ruff line 100.\n")
    (repo / "server.py").write_bytes(b"def real_fn():\n    pass\n" * 20)
    (repo / "test_server.py").write_bytes(b"def test_real_fn():\n    pass\n" * 10)
    plan = repo / "plan.md"
    plan.write_text(PLAN_TEXT)
    return repo, plan


def codegraph_response(args):
    """Canned codegraph CLI output covering every command the pipeline issues."""
    command = args[0]
    if command == "files":
        return "repo files: server.py, test_server.py"
    symbol = args[1]
    if command == "query":
        if symbol in ("real_fn", "test_real_fn"):
            return json.dumps([{"node": {"name": symbol, "filePath": "server.py"}}])
        return json.dumps([])
    if command == "callers" and symbol == "real_fn":
        return json.dumps(
            {
                "symbol": symbol,
                "callers": [
                    {"name": "test_real_fn", "kind": "function", "filePath": "test_server.py"}
                ],
            }
        )
    key = {"callers": "callers", "callees": "callees", "impact": "affected"}[command]
    return json.dumps({"symbol": symbol, key: []})


MAPPER_RESPONSE = json.dumps(
    {
        "tasks": [
            {
                "task_id": "t1-proxy",
                "description": "extend the proxy server tool list",
                "files": ["server.py"],
                "symbols": ["real_fn"],
            },
            {
                "task_id": "t2-tests",
                "description": "cover the proxy with tests",
                "files": ["test_server.py"],
                "symbols": ["test_real_fn"],
            },
        ]
    }
)


def speccer_response(prompt, schema):
    """Echoes a valid spec for every group id present in the prompt."""
    # raw_decode tolerates the corrective nudge a retry appends after the JSON blob
    payload, _ = json.JSONDecoder().raw_decode(prompt.split("GROUPS_JSON:\n", 1)[1])
    group_ids = sorted(payload.keys())
    return json.dumps(
        {
            "groups": [
                {
                    "group_id": gid,
                    "name": f"group-{gid}",
                    "summary": f"Summary for {gid}",
                    "spec": f"Full spec for {gid}.",
                    "verification": [
                        {"id": f"{gid}-v1", "description": "tests pass", "required": True}
                    ],
                }
                for gid in group_ids
            ]
        }
    )


class StubLlm:
    """Dispatches on the JSON schema title — the pipeline's two LLM seams."""

    def __init__(self, mapper=MAPPER_RESPONSE, speccer=speccer_response):
        self.mapper = mapper
        self.speccer = speccer
        self.prompts = []

    def __call__(self, prompt, schema):
        self.prompts.append((schema.get("title"), prompt))
        if schema.get("title") == "mapper_output":
            return self.mapper if isinstance(self.mapper, str) else self.mapper(prompt, schema)
        return self.speccer if isinstance(self.speccer, str) else self.speccer(prompt, schema)


def make_client(repo):
    return CodegraphClient(repo_root=repo, runner=codegraph_response)


def grouping(tmp_path, llm=None, config=None):
    repo, plan = make_repo(tmp_path)
    result, base_context = run_grouping(
        plan_path=plan,
        repo_root=repo,
        config=config or OrchestratorConfig(),
        llm_runner=llm or StubLlm(),
        client=make_client(repo),
    )
    return result, base_context


class TestCallLlmJson:
    def schema(self):
        return {"title": "thing", "type": "object", "properties": {"x": {"type": "integer"}}}

    def test_valid_json_passes_through_validator(self):
        result = call_llm_json(
            lambda p, s: '{"x": 1}', "prompt", self.schema(), validate=lambda d: d["x"]
        )
        assert result == 1

    def test_invalid_json_retries_with_corrective_nudge(self):
        calls = []

        def flaky(prompt, schema):
            calls.append(prompt)
            return "garbage {" if len(calls) == 1 else '{"x": 2}'

        result = call_llm_json(flaky, "prompt", self.schema(), validate=lambda d: d["x"])
        assert result == 2
        assert len(calls) == 2
        assert "failed validation" in calls[1]

    def test_persistent_failure_aborts_and_saves_raw_output(self, tmp_path):
        """Plan U4 scenario: persistent failure aborts with raw output saved."""
        with pytest.raises(LlmError, match="thing"):
            call_llm_json(
                lambda p, s: "never json",
                "prompt",
                self.schema(),
                validate=lambda d: d,
                max_retries=2,
                failure_dir=tmp_path,
            )
        saved = list(tmp_path.glob("*.txt"))
        assert saved and "never json" in saved[0].read_text()


class TestMapperVerification:
    def test_hallucinated_symbol_dropped_flagged_task_still_grouped(self, tmp_path):
        """Plan U4 scenario: nonexistent symbol dropped, flagged, task still lands."""
        mapper = json.loads(MAPPER_RESPONSE)
        mapper["tasks"][0]["symbols"] = ["real_fn", "ghost_fn"]
        result, _ = grouping(tmp_path, llm=StubLlm(mapper=json.dumps(mapper)))
        assert any("ghost_fn" in flag for flag in result.flags)
        grouped_tasks = {task for group in result.groups for task in group.tasks}
        assert "t1-proxy" in grouped_tasks

    def test_nonexistent_file_dropped_and_flagged(self, tmp_path):
        mapper = json.loads(MAPPER_RESPONSE)
        mapper["tasks"][0]["files"] = ["server.py", "imaginary/nope.py"]
        result, _ = grouping(tmp_path, llm=StubLlm(mapper=json.dumps(mapper)))
        assert any("imaginary/nope.py" in flag for flag in result.flags)

    def test_unmappable_task_rides_along_as_regionless_node(self, tmp_path):
        """Region-less tasks join via prose-affinity fallback instead of vanishing."""
        mapper = json.loads(MAPPER_RESPONSE)
        mapper["tasks"].append(
            {"task_id": "t3-docs", "description": "update the README", "files": [], "symbols": []}
        )
        result, _ = grouping(tmp_path, llm=StubLlm(mapper=json.dumps(mapper)))
        grouped_tasks = {task for group in result.groups for task in group.tasks}
        assert "t3-docs" in grouped_tasks
        assert any("t3-docs" in flag for flag in result.flags)

    def test_mapper_returning_no_tasks_aborts(self, tmp_path):
        with pytest.raises(GrouperError, match="no tasks"):
            grouping(tmp_path, llm=StubLlm(mapper=json.dumps({"tasks": []})))

    def test_mapper_nonlist_files_triggers_validation_retry(self, tmp_path):
        """A bare string for 'files' must hit the retry path, not be iterated
        character by character."""
        bad = json.dumps(
            {"tasks": [{"task_id": "t", "description": "d", "files": "server.py", "symbols": []}]}
        )
        llm = StubLlm(mapper=bad)
        with pytest.raises(LlmError):
            grouping(tmp_path, llm=llm)
        mapper_prompts = [p for title, p in llm.prompts if title == "mapper_output"]
        assert len(mapper_prompts) >= 2
        assert "failed validation" in mapper_prompts[1]

    def test_adjacent_regionless_tasks_do_not_double_prose_affinity(self):
        """Two adjacent region-less tasks nominate the same pair from both sides;
        the fallback weight must be applied once."""
        from orchestrator.grouping.graphing import TaskMapping
        from orchestrator.grouping.partition import TaskGraph
        from orchestrator.grouping.pipeline import _with_prose_fallback

        mapper_out = MapperOutput(
            mappings=[TaskMapping("t1"), TaskMapping("t2"), TaskMapping("t3", files=("a.py",))],
            descriptions={},
        )
        graph = TaskGraph(nodes=frozenset({"t1", "t2", "t3"}))
        patched = _with_prose_fallback(graph, mapper_out, weight=0.5)
        assert patched.affinity == {("t1", "t2"): 0.5}


class TestPipeline:
    def test_ae1_cohesive_plan_yields_exactly_one_group(self, tmp_path):
        """AE1: whole plan fits the budget → one group, clean short-circuit."""
        result, _ = grouping(tmp_path)
        assert len(result.groups) == 1
        group = result.groups[0]
        assert group.id == "g1"
        assert group.dependencies == []
        assert sorted(group.tasks) == ["t1-proxy", "t2-tests"]
        assert group.spec and group.summary and group.name
        assert group.estimated_tokens > 0

    def test_groups_carry_r6_contract_fields(self, tmp_path):
        result, _ = grouping(tmp_path)
        group = result.groups[0]
        assert group.verification and group.verification[0].id
        assert 0.0 <= group.difficulty <= 1.0
        assert group.intensity is not None
        assert group.files

    def test_oversummary_rejected_at_validation_not_truncated(self, tmp_path):
        """Plan U4 scenario: summaries over the length bound are rejected."""

        def long_summary(prompt, schema):
            payload = json.loads(speccer_response(prompt, schema))
            for entry in payload["groups"]:
                entry["summary"] = "x" * 200
            return json.dumps(payload)

        with pytest.raises(LlmError):
            grouping(tmp_path, llm=StubLlm(speccer=long_summary))

    def test_speccer_schema_failure_retries_then_aborts(self, tmp_path):
        """Plan U4 scenario: invalid speccer JSON triggers a bounded retry nudge."""
        llm = StubLlm(speccer="not json at all")
        with pytest.raises(LlmError):
            grouping(tmp_path, llm=llm)
        speccer_prompts = [p for title, p in llm.prompts if title == "speccer_output"]
        assert len(speccer_prompts) >= 2
        assert "failed validation" in speccer_prompts[1]

    def test_group_estimate_counts_shared_file_once(self, tmp_path):
        """A file shared by several member tasks is sized once for the group
        estimate, not once per task."""
        mapper = json.dumps(
            {
                "tasks": [
                    {"task_id": "t1", "description": "a", "files": ["server.py"], "symbols": []},
                    {"task_id": "t2", "description": "b", "files": ["server.py"], "symbols": []},
                ]
            }
        )
        result, base_context = grouping(tmp_path, llm=StubLlm(mapper=mapper))
        assert len(result.groups) == 1
        group = result.groups[0]
        config = OrchestratorConfig().estimator
        expected = estimate_group_tokens(
            source_bytes=(tmp_path / "repo" / "server.py").stat().st_size,
            file_count=1,
            spec_tokens=int(len(group.spec) / config.bytes_per_token),
            base_tokens=int(len(base_context) / config.bytes_per_token),
            config=config,
        )
        assert group.estimated_tokens == expected

    def test_determinism_byte_identical_output(self, tmp_path):
        """Plan U4 scenario: same plan + fixtures → byte-identical groups.json and
        base context."""
        result_a, context_a = grouping(tmp_path)
        result_b, context_b = grouping(tmp_path)
        assert serialize_grouping(result_a) == serialize_grouping(result_b)
        assert context_a == context_b


class TestBaseContext:
    def test_compiles_conventions_plan_and_codegraph_summary(self, tmp_path):
        repo, plan = make_repo(tmp_path)
        text = compile_base_context(repo, plan, codegraph_summary="ARCH SUMMARY")
        assert "Use ruff line 100." in text
        assert "toy plan" in text
        assert "ARCH SUMMARY" in text

    def test_missing_convention_files_tolerated(self, tmp_path):
        repo, plan = make_repo(tmp_path)
        (repo / "CLAUDE.md").unlink()
        text = compile_base_context(repo, plan, codegraph_summary="")
        assert "toy plan" in text

    def test_byte_stable(self, tmp_path):
        repo, plan = make_repo(tmp_path)
        assert compile_base_context(repo, plan, "s") == compile_base_context(repo, plan, "s")


class TestDryRunCli:
    def test_group_dry_run_prints_groups_and_estimates(self, tmp_path, capsys):
        """Plan U4 verification: dry-run prints groups, DAG, estimates, and flags."""
        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--dry-run"],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "g1" in out
        assert "tokens" in out.lower()
        assert not (repo / ".orchestrator").exists()

    def test_group_writes_artifacts_without_dry_run(self, tmp_path):
        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo)],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0
        assert (repo / ".orchestrator" / "groups.json").is_file()
        assert (repo / ".orchestrator" / "base-context.md").is_file()

    def test_malformed_config_fails_cleanly(self, tmp_path, capsys):
        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path)
        config_dir = repo / ".orchestrator"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text('[estimator]\ntoken_budget = "lots"\n')
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--dry-run"],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 1
        assert "invalid config" in capsys.readouterr().err

    def test_unknown_plan_path_fails_actionably(self, tmp_path, capsys):
        from orchestrator.cli import main

        exit_code = main(
            ["group", str(tmp_path / "missing.md"), "--repo", str(tmp_path)],
            llm_runner=StubLlm(),
        )
        assert exit_code != 0
        assert "plan" in capsys.readouterr().err.lower()
