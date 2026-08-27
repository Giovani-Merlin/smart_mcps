"""Tests for the U4 grouper pipeline — LLM at the edges, deterministic core.

Written against the pipeline seams (plan U4 execution note): both LLM calls are
stubbed through the runner seam, codegraph through the client runner seam, and
determinism is asserted on the serialized output.
"""

import json
import subprocess

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.grouping.base_context import compile_base_context
from orchestrator.grouping.graphing import CodegraphClient
from orchestrator.grouping.estimator import estimate_group_tokens, partition_budget_cap
from orchestrator.config import SessionConfig
from orchestrator.grouping import llm as llm_module
from orchestrator.grouping.llm import LlmError, call_llm_json
from orchestrator.grouping.mapper import MapperOutput
from orchestrator.grouping.pipeline import (
    GrouperError,
    compute_partition,
    run_grouping,
    serialize_grouping,
)
from orchestrator.model import Group, ReviewIntensity, Surprise

PLAN_TEXT = """# feat: toy plan

## Tasks

- T1: extend the proxy server tool list
- T2: cover the proxy with tests
"""

GREENFIELD_PLAN = """# feat: greenfield service

## Units

- t1-scaffold: create the app skeleton
- t2-items-api: items API routes
- t3-items-ui: items admin page
- t4-docs: usage docs

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: t1-scaffold
    description: create the app skeleton
    files: [app/main.py]
  - task_id: t2-items-api
    description: items API routes
    slice: items
    files: [app/items.py]
    depends_on: [t1-scaffold]
    implements: ["/api/items"]
  - task_id: t3-items-ui
    description: items admin page
    slice: items
    files: [web/items.tsx]
    depends_on: [t1-scaffold]
    consumes: ["/api/items"]
  - task_id: t4-docs
    description: usage docs
    files: [docs/usage.md]
    depends_on: [t1-scaffold]
```
"""

MIXED_PLAN = """# feat: cross-stack feature on the existing server

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: t1-api
    description: extend the existing server with the items route
    files: [server.py]
    symbols: [real_fn]
    implements: ["/api/items"]
  - task_id: t2-ui
    description: new admin page consuming the items route
    files: [web/items.tsx]
    consumes: ["/api/items"]
```
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
    if command == "sync":
        return ""
    if command == "files":
        return "repo files: server.py, test_server.py"
    if command == "status":
        return json.dumps(
            {
                "initialized": True,
                "fileCount": 2,
                "nodeCount": 4,
                "edgeCount": 1,
                "pendingChanges": {"added": 0, "modified": 0, "removed": 0},
            }
        )
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


def grouping(tmp_path, llm=None, config=None, allow_unknown_symbols=False):
    repo, plan = make_repo(tmp_path)
    result, base_context = run_grouping(
        plan_path=plan,
        repo_root=repo,
        config=config or OrchestratorConfig(),
        llm_runner=llm or StubLlm(),
        client=make_client(repo),
        allow_unknown_symbols=allow_unknown_symbols,
    )
    return result, base_context


class TestOrchestratorThinkingPolicy:
    """The orchestrator's own reasoning calls get a larger thinking budget than
    workers do: a bad partition costs every group downstream, and there are only a
    handful of these per run."""

    def test_mapper_speccer_calls_pin_adaptive_thinking_at_the_high_budget(self, monkeypatch):
        captured: list[list[str]] = []

        def fake_run(argv, **kwargs):
            captured.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout='{"result": "{}"}', stderr="")

        monkeypatch.setattr(llm_module.subprocess, "run", fake_run)
        llm_module.claude_json_runner("prompt", {"type": "object"})

        (argv,) = captured
        assert argv[argv.index("--thinking") + 1] == "adaptive"
        assert argv[argv.index("--max-thinking-tokens") + 1] == "10000"
        # Strictly above the workers' medium budget, or this policy is pointless.
        assert llm_module.ORCHESTRATOR_MAX_THINKING_TOKENS > SessionConfig().max_thinking_tokens


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


class TestTaskMapRegimes:
    """The two live smoke1 failures plus the compatibility regimes, now assertable
    (plan U6): greenfield keeps structure, cross-stack halves co-group, foreign
    plans keep the mapper, malformed maps fail loudly before any LLM call."""

    def run_plan(self, tmp_path, plan_text, llm=None):
        repo, plan = make_repo(tmp_path)
        plan.write_text(plan_text)
        llm = llm or StubLlm()
        result, base_context = run_grouping(
            plan_path=plan, repo_root=repo, llm_runner=llm, client=make_client(repo)
        )
        return result, llm

    def test_pure_greenfield_yields_ordered_groups_not_independents(self, tmp_path):
        """Regime (a): all-prospective plan with scaffold→consumer deps produces
        clustered, dependency-ordered groups — the smoke1 hand-edit, automated."""
        result, llm = self.run_plan(tmp_path, GREENFIELD_PLAN)
        by_task = {task: group for group in result.groups for task in group.tasks}
        # slice-mates co-grouped (contraction + the semantic route-tag edge)
        assert by_task["t2-items-api"].id == by_task["t3-items-ui"].id
        # ordered groups, not N independents: everything hangs off the scaffold
        assert len(result.groups) >= 2
        scaffold_gid = by_task["t1-scaffold"].id
        for group in result.groups:
            if group.id != scaffold_gid:
                assert scaffold_gid in group.dependencies
        # prospective files reach Group.files — workers must create them
        assert "app/items.py" in by_task["t2-items-api"].files
        assert any("retained as prospective" in flag for flag in result.flags)

    def test_premapped_plan_invokes_runner_only_for_the_speccer(self, tmp_path):
        """Regime (d): the mapper LLM is skipped and flagged."""
        result, llm = self.run_plan(tmp_path, GREENFIELD_PLAN)
        assert result.flags[0] == "task map: parsed from plan — mapper LLM skipped"
        titles = [title for title, _ in llm.prompts]
        assert titles and set(titles) == {"speccer_output"}

    def test_plan_without_task_map_keeps_the_mapper_path(self, tmp_path):
        """Regime (b): foreign plans work unchanged — mapper called, no skip flag."""
        result, llm = self.run_plan(tmp_path, PLAN_TEXT)
        assert any(title == "mapper_output" for title, _ in llm.prompts)
        assert not any(flag.startswith("task map:") for flag in result.flags)

    def test_mixed_plan_co_groups_cross_stack_halves(self, tmp_path):
        """Regime (c): an existing-code task and a greenfield task joined only by
        route tags land in one group; structural and semantic layers coexist."""
        result, _ = self.run_plan(tmp_path, MIXED_PLAN)
        by_task = {task: group for group in result.groups for task in group.tasks}
        assert by_task["t1-api"].id == by_task["t2-ui"].id
        files = by_task["t1-api"].files
        assert "server.py" in files and "web/items.tsx" in files

    def test_malformed_task_map_fails_loudly_with_zero_llm_calls(self, tmp_path):
        """Regime (e): a broken block must never silently fall back to the mapper
        (that would hide prose↔map drift)."""
        repo, plan = make_repo(tmp_path)
        plan.write_text("# feat: x\n\n```yaml\n# orchestrator-task-map v1\ntasks: [unclosed\n```\n")
        llm = StubLlm()
        with pytest.raises(GrouperError, match="task map"):
            run_grouping(plan_path=plan, repo_root=repo, llm_runner=llm, client=make_client(repo))
        assert llm.prompts == []

    def test_premapped_output_is_byte_deterministic(self, tmp_path):
        result_a, _ = self.run_plan(tmp_path, GREENFIELD_PLAN)
        result_b, _ = self.run_plan(tmp_path, GREENFIELD_PLAN)
        assert serialize_grouping(result_a) == serialize_grouping(result_b)

    def test_prospective_file_task_gets_no_prose_fallback(self):
        """A task with prospective files is not region-less — its planned files
        already carry real shared-file affinity."""
        from orchestrator.grouping.graphing import TaskMapping
        from orchestrator.grouping.partition import TaskGraph
        from orchestrator.grouping.pipeline import _with_prose_fallback

        mapper_out = MapperOutput(
            mappings=[
                TaskMapping("t1", prospective_files=("new.py",)),
                TaskMapping("t2"),
                TaskMapping("t3", files=("a.py",)),
            ],
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
        # Plan U9: dry-run still writes the trace — it's the only artifact a
        # dry run leaves — but not groups.json or base-context.md.
        assert (repo / ".orchestrator" / "groupings" / "plan" / "grouping-trace.json").is_file()
        assert not (repo / ".orchestrator" / "groupings" / "plan" / "groups.json").exists()
        assert not (repo / ".orchestrator" / "groupings" / "plan" / "base-context.md").exists()

    def test_group_writes_artifacts_without_dry_run(self, tmp_path):
        """Plan U10: with no --name, the grouping directory is named after the
        plan's filename stem (``plan.md`` -> ``plan``), and no top-level
        artifact is written any more."""
        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo)],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0
        assert (repo / ".orchestrator" / "groupings" / "plan" / "groups.json").is_file()
        assert (repo / ".orchestrator" / "groupings" / "plan" / "base-context.md").is_file()
        assert not (repo / ".orchestrator" / "groups.json").exists()

    def test_group_with_explicit_name_writes_under_that_directory(self, tmp_path):
        """Plan U10: `--name` picks the grouping directory explicitly."""
        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--name", "alpha"],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0
        assert (repo / ".orchestrator" / "groupings" / "alpha" / "groups.json").is_file()
        assert (repo / ".orchestrator" / "groupings" / "alpha" / "base-context.md").is_file()
        assert not (repo / ".orchestrator" / "groupings" / "plan").exists()

    def test_group_name_with_path_separator_is_rejected_before_writing(self, tmp_path, capsys):
        """Plan U10: a `--name` containing a path separator or `..` is rejected
        before anything is written under .orchestrator/."""
        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--name", "../escape"],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code != 0
        assert "invalid grouping name" in capsys.readouterr().err
        assert not (repo / ".orchestrator").exists()

    def test_group_name_with_dotdot_alone_is_rejected(self, tmp_path, capsys):
        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--name", ".."],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code != 0
        assert "invalid grouping name" in capsys.readouterr().err
        assert not (repo / ".orchestrator").exists()

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


def _llm_must_not_be_called(prompt, schema):
    raise AssertionError("the LLM runner must not be called for this scenario")


class TestComputePartition:
    """U7 (R19): the deterministic prefix of run_grouping, callable standalone —
    mapper -> graph -> partition -> group DAG, with the R18 report fields."""

    def test_returns_every_r18_field(self, tmp_path):
        repo, plan = make_repo(tmp_path)
        plan.write_text(GREENFIELD_PLAN)
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=_llm_must_not_be_called,
            client=make_client(repo),
        )
        assert set(outcome.partition) == set(outcome.graph.nodes)
        assert outcome.dag == {0: {1}}
        assert outcome.node_work and all(v >= 0 for v in outcome.node_work.values())
        assert outcome.budget_cap > 0
        assert outcome.hub_roles["t1-scaffold"] == "utility_hub"
        assert outcome.slice_atoms == {"items": ["t2-items-api", "t3-items-ui"]}
        assert outcome.last_stage in {"contraction", "louvain", "lift", "split", "merge"}
        assert "greenfield service" in outcome.base_context

    def test_task_map_plan_never_calls_the_llm_runner(self, tmp_path):
        """The R19 seam is sub-second and zero-LLM whenever a task map is present —
        the mapper is skipped entirely, so a raising stub must never fire."""
        repo, plan = make_repo(tmp_path)
        plan.write_text(GREENFIELD_PLAN)
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=_llm_must_not_be_called,
            client=make_client(repo),
        )
        assert outcome.mapper_out.flags[0] == "task map: parsed from plan — mapper LLM skipped"

    def test_run_grouping_partition_matches_compute_partition_alone(self, tmp_path):
        """run_grouping is compute_partition + speccer + assembly — the partition
        it hands to the speccer must be exactly what compute_partition returns."""
        repo, plan = make_repo(tmp_path)
        plan.write_text(GREENFIELD_PLAN)
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=_llm_must_not_be_called,
            client=make_client(repo),
        )
        result, _ = run_grouping(
            plan_path=plan, repo_root=repo, llm_runner=StubLlm(), client=make_client(repo)
        )
        by_task = {task: group.id for group in result.groups for task in group.tasks}
        members_by_gid: dict[int, list[str]] = {}
        for node, gid in outcome.partition.items():
            members_by_gid.setdefault(gid, []).append(node)
        for gid, members in members_by_gid.items():
            group_ids = {by_task[m] for m in members}
            assert len(group_ids) == 1


class TestNoSpecCli:
    """U7 (R18): `group <plan> --no-spec` — the zero-LLM, sub-second report."""

    def test_no_spec_prints_r18_items_and_never_calls_the_llm(self, tmp_path, capsys):
        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path)
        plan.write_text(GREENFIELD_PLAN)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            llm_runner=_llm_must_not_be_called,
            client=make_client(repo),
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        for expected in (
            "node work",
            "budget cap",
            "hub roles",
            "slice atoms",
            "last partition-modifying stage",
            "depends on",
        ):
            assert expected in out
        assert not (repo / ".orchestrator" / "groups.json").exists()
        assert not (repo / ".orchestrator" / "groupings" / "plan" / "groups.json").exists()
        assert not (repo / ".orchestrator" / "groupings" / "plan" / "base-context.md").exists()
        assert (repo / ".orchestrator" / "groupings" / "plan" / "grouping-trace.json").is_file()

    def test_no_spec_completes_in_under_a_second(self, tmp_path):
        import time

        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path)
        plan.write_text(GREENFIELD_PLAN)
        start = time.monotonic()
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            llm_runner=_llm_must_not_be_called,
            client=make_client(repo),
        )
        elapsed = time.monotonic() - start
        assert exit_code == 0
        assert elapsed < 1.0


class TestGroupingTraceArtifact:
    """U9: grouping-trace.json is written in every `group` mode, including
    failure, and --no-spec's report is byte-for-byte reproducible from it."""

    def test_full_group_writes_trace_alongside_groups_json(self, tmp_path):
        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo)],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0
        grouping_dir = repo / ".orchestrator" / "groupings" / "plan"
        assert (grouping_dir / "groups.json").is_file()
        assert (grouping_dir / "base-context.md").is_file()
        assert (grouping_dir / "grouping-trace.json").is_file()

    def test_no_spec_trace_is_byte_identical_across_repeated_runs(self, tmp_path):
        """Plan U5 (added 2026-07-30): provenance.timestamp is the one field in
        the whole trace expected to differ run to run by design — it records
        *when* grouping ran. Parsed back and compared with that one leaf
        excluded, so real content drift (including elsewhere in provenance,
        e.g. the index fingerprint) would still fail this test."""
        from orchestrator.cli import main
        from orchestrator.grouping.trace import GroupingTrace

        repo, plan = make_repo(tmp_path)
        plan.write_text(GREENFIELD_PLAN)
        trace_path = repo / ".orchestrator" / "groupings" / "plan" / "grouping-trace.json"

        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            llm_runner=_llm_must_not_be_called,
            client=make_client(repo),
        )
        assert exit_code == 0
        first = GroupingTrace.model_validate_json(trace_path.read_text())

        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            llm_runner=_llm_must_not_be_called,
            client=make_client(repo),
        )
        assert exit_code == 0
        second = GroupingTrace.model_validate_json(trace_path.read_text())

        exclude = {"provenance": {"timestamp"}}
        assert first.model_dump(exclude=exclude) == second.model_dump(exclude=exclude)

    def test_failing_group_still_writes_a_trace_naming_the_failure(self, tmp_path, capsys):
        """A real, existing failure mode (an empty task map) exercises the same
        _write_failure_trace path a future slice-overflow GrouperError (plan
        U6) will also raise through — the CLI message points at the file and
        the trace's failure section carries the exception verbatim."""
        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path)
        plan.write_text(
            "# feat: empty task map\n\n## Task Map\n\n```yaml\n"
            "# orchestrator-task-map v1\ntasks: []\n```\n"
        )
        trace_path = repo / ".orchestrator" / "groupings" / "plan" / "grouping-trace.json"
        exit_code = main(
            ["group", str(plan), "--repo", str(repo)],
            llm_runner=_llm_must_not_be_called,
            client=make_client(repo),
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert str(trace_path) in err
        assert trace_path.is_file()
        from orchestrator.grouping.trace import GroupingTrace

        trace = GroupingTrace.model_validate_json(trace_path.read_text())
        assert trace.failure is not None
        assert trace.failure.kind == "GrouperError"
        assert "non-empty list" in trace.failure.message

    def test_slice_overflow_shaped_failure_message_is_captured_verbatim(
        self, tmp_path, monkeypatch
    ):
        """Stands in for U6's not-yet-landed --allow-oversized-slice gate: the
        failure-trace path is generic over *any* GrouperError, so whatever
        message that gate raises (naming the slice, its members, the cap, and
        the overshoot) will be captured exactly like this one is."""
        import orchestrator.cli as cli_module
        from orchestrator.cli import main
        from orchestrator.grouping.pipeline import GrouperError

        repo, plan = make_repo(tmp_path)

        def raise_slice_overflow(*args, **kwargs):
            raise GrouperError(
                "slice 'reports' (members: reports-api, reports-ui) totals 12000 "
                "work against a cap of 8000 (4000 over)"
            )

        monkeypatch.setattr(cli_module, "run_grouping", raise_slice_overflow)
        trace_path = repo / ".orchestrator" / "groupings" / "plan" / "grouping-trace.json"
        exit_code = main(
            ["group", str(plan), "--repo", str(repo)],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 1
        from orchestrator.grouping.trace import GroupingTrace

        trace = GroupingTrace.model_validate_json(trace_path.read_text())
        assert trace.failure is not None
        assert "reports" in trace.failure.message
        assert "8000" in trace.failure.message


class TestSyncGate:
    """R13: `group` refuses to run against a stale index — sync() is invoked,
    blocking, before the first index read (files_overview)."""

    def test_sync_runs_before_files_overview(self, tmp_path):
        repo, plan = make_repo(tmp_path)
        plan.write_text(GREENFIELD_PLAN)
        calls = []

        def recording_runner(args):
            calls.append(list(args))
            return codegraph_response(args)

        client = CodegraphClient(repo_root=repo, runner=recording_runner)
        compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=_llm_must_not_be_called,
            client=client,
        )
        assert calls[0] == ["sync"]
        assert calls[1][0] == "files"

    def test_run_grouping_also_syncs_first(self, tmp_path):
        repo, plan = make_repo(tmp_path)
        plan.write_text(GREENFIELD_PLAN)
        calls = []

        def recording_runner(args):
            calls.append(list(args))
            return codegraph_response(args)

        client = CodegraphClient(repo_root=repo, runner=recording_runner)
        run_grouping(plan_path=plan, repo_root=repo, llm_runner=StubLlm(), client=client)
        assert calls[0] == ["sync"]
        assert calls[1][0] == "files"


UNKNOWN_SYMBOL_PLAN = """# feat: proxy task naming an unknown symbol

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: t1-proxy
    description: extend the proxy server tool list
    files: [server.py]
    symbols: [real_fn, ghost_fn]
```
"""


class TestUnknownSymbolGate:
    """R14: an unknown task-map symbol is a hard error by default;
    --allow-unknown-symbols restores drop-with-flag. The mapper-fallback path
    (no task map) keeps drop-with-flag regardless of the flag."""

    def test_unknown_symbol_raises_grouper_error_naming_task_and_symbol(self, tmp_path):
        repo, plan = make_repo(tmp_path)
        plan.write_text(UNKNOWN_SYMBOL_PLAN)
        with pytest.raises(GrouperError, match=r"t1-proxy.*ghost_fn"):
            run_grouping(
                plan_path=plan, repo_root=repo, llm_runner=StubLlm(), client=make_client(repo)
            )

    def test_allow_unknown_symbols_flag_restores_drop_with_flag(self, tmp_path):
        repo, plan = make_repo(tmp_path)
        plan.write_text(UNKNOWN_SYMBOL_PLAN)
        result, _ = run_grouping(
            plan_path=plan,
            repo_root=repo,
            llm_runner=StubLlm(),
            client=make_client(repo),
            allow_unknown_symbols=True,
        )
        assert any("ghost_fn" in flag and "dropped" in flag for flag in result.flags)
        grouped_tasks = {task for group in result.groups for task in group.tasks}
        assert "t1-proxy" in grouped_tasks

    def test_cli_allow_unknown_symbols_flag(self, tmp_path, capsys):
        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path)
        plan.write_text(UNKNOWN_SYMBOL_PLAN)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--dry-run"],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 1
        assert "ghost_fn" in capsys.readouterr().err

        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--dry-run", "--allow-unknown-symbols"],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0
        assert "ghost_fn" in capsys.readouterr().out

    def test_prospective_files_unaffected_by_the_flag(self, tmp_path):
        """A plan can carry both a claimed symbol and a not-yet-created file;
        the flag only governs symbol handling, so prospective-file treatment
        must match in both regimes."""
        plan_text = (
            "# feat: mixed prospective file and unknown symbol\n\n"
            "## Task Map\n\n```yaml\n"
            "# orchestrator-task-map v1\n"
            "tasks:\n"
            "  - task_id: t1\n"
            "    description: d\n"
            "    files: [server.py, brand/new.py]\n"
            "    symbols: [real_fn, ghost_fn]\n"
            "```\n"
        )
        repo, plan = make_repo(tmp_path)
        plan.write_text(plan_text)
        result, _ = run_grouping(
            plan_path=plan,
            repo_root=repo,
            llm_runner=StubLlm(),
            client=make_client(repo),
            allow_unknown_symbols=True,
        )
        assert "brand/new.py" in result.groups[0].files
        assert any("brand/new.py" in flag and "prospective" in flag for flag in result.flags)

    def test_mapper_fallback_keeps_drop_with_flag_regardless_of_the_flag(self, tmp_path):
        mapper = json.loads(MAPPER_RESPONSE)
        mapper["tasks"][0]["symbols"] = ["real_fn", "ghost_fn"]
        for allow in (False, True):
            result, _ = grouping(
                tmp_path,
                llm=StubLlm(mapper=json.dumps(mapper)),
                allow_unknown_symbols=allow,
            )
            assert any("ghost_fn" in flag for flag in result.flags)
            grouped_tasks = {task for group in result.groups for task in group.tasks}
            assert "t1-proxy" in grouped_tasks


SELF_MOD_PLAN = """# feat: touch the orchestrator itself

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: t1-cli
    description: tweak the CLI banner
    files: [orchestrator/cli.py]
```
"""


class TestSelfModificationWarning:
    """R15: a plan whose mappings touch orchestrator/ gets flagged and warned
    about at grouping time — D12's worker-changes-land-next-run rule."""

    def test_flag_and_stderr_warning_when_plan_touches_orchestrator(self, tmp_path, capsys):
        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path)
        plan.write_text(SELF_MOD_PLAN)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--dry-run"],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0
        err = capsys.readouterr().err
        assert "take effect on the next run" in err

    def test_no_warning_when_plan_does_not_touch_orchestrator(self, tmp_path, capsys):
        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path)
        plan.write_text(GREENFIELD_PLAN)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--dry-run"],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0
        assert capsys.readouterr().err == ""

    def test_no_spec_path_also_flags_and_warns(self, tmp_path, capsys):
        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path)
        plan.write_text(SELF_MOD_PLAN)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            llm_runner=_llm_must_not_be_called,
            client=make_client(repo),
        )
        assert exit_code == 0
        assert "take effect on the next run" in capsys.readouterr().err


class TestTaskMapStripping:
    """R27: the task-map YAML block is grouper parser input only — it never
    reaches an LLM-facing context (base context, speccer prompt, rewrite prompt).
    The plan file on disk is never touched."""

    def test_base_context_has_no_marker_or_heading(self, tmp_path):
        repo, plan = make_repo(tmp_path)
        plan.write_text(GREENFIELD_PLAN)
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=_llm_must_not_be_called,
            client=make_client(repo),
        )
        assert "orchestrator-task-map v1" not in outcome.base_context
        assert "## Task Map" not in outcome.base_context
        # the surrounding unit prose survives the strip
        assert "t1-scaffold: create the app skeleton" in outcome.base_context

    def test_base_context_compilation_stays_byte_stable(self, tmp_path):
        repo, plan = make_repo(tmp_path)
        plan.write_text(GREENFIELD_PLAN)
        first = compile_base_context(repo, plan, codegraph_summary="s")
        second = compile_base_context(repo, plan, codegraph_summary="s")
        assert first == second

    def test_budget_cap_is_derived_from_the_stripped_base_context(self, tmp_path):
        repo, plan = make_repo(tmp_path)
        plan.write_text(GREENFIELD_PLAN)
        config = OrchestratorConfig()
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            config=config,
            llm_runner=_llm_must_not_be_called,
            client=make_client(repo),
        )
        expected_tokens = int(len(outcome.base_context) / config.estimator.bytes_per_token)
        assert outcome.base_tokens == expected_tokens
        assert outcome.budget_cap == partition_budget_cap(expected_tokens, config.estimator)

    def test_speccer_prompt_has_no_version_marker(self, tmp_path):
        repo, plan = make_repo(tmp_path)
        plan.write_text(GREENFIELD_PLAN)
        llm = StubLlm()
        run_grouping(plan_path=plan, repo_root=repo, llm_runner=llm, client=make_client(repo))
        speccer_prompts = [p for title, p in llm.prompts if title == "speccer_output"]
        assert speccer_prompts
        assert all("orchestrator-task-map v1" not in p for p in speccer_prompts)

    def test_rewrite_prompt_has_no_version_marker(self, tmp_path):
        from orchestrator.cli import _rewrite_provider
        from orchestrator.grouping.plan_reader import strip_task_map

        # sanity: the raw plan text does carry the marker, so the assertion below
        # is meaningful — it is the strip that removes it, not something else.
        assert "orchestrator-task-map v1" in GREENFIELD_PLAN
        stripped = strip_task_map(GREENFIELD_PLAN)
        llm = StubLlm()
        rewrite_spec = _rewrite_provider(stripped, llm, failure_dir=tmp_path)
        group = Group(
            id="g1",
            name="n",
            summary="s",
            spec="old spec",
            difficulty=0.1,
            intensity=ReviewIntensity.SELF_VERIFY,
            tasks=["t1-scaffold"],
            files=["app/main.py"],
        )
        rewrite_spec(group, surprises=[Surprise(kind="other", description="stuck")])
        prompts = [p for _, p in llm.prompts]
        assert prompts
        assert all("orchestrator-task-map v1" not in p for p in prompts)

    def test_plan_file_on_disk_is_byte_identical_after_group(self, tmp_path):
        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path)
        plan.write_text(GREENFIELD_PLAN)
        before = plan.read_bytes()
        exit_code = main(
            ["group", str(plan), "--repo", str(repo)],
            llm_runner=StubLlm(),
            client=make_client(repo),
        )
        assert exit_code == 0
        assert plan.read_bytes() == before


class TestStageProgress:
    """Plan U24: a `group` invocation streams stage/spec lines through the
    ``progress`` seam instead of staying silent for the length of the run."""

    def test_no_spec_path_emits_mapper_graph_partition_stages_in_order(self, tmp_path):
        repo, plan = make_repo(tmp_path)
        lines: list[str] = []
        compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=StubLlm(),
            client=make_client(repo),
            progress=lines.append,
        )
        assert lines == ["stage: mapper", "stage: graph", "stage: partition"]

    def test_full_pipeline_emits_one_spec_line_per_group(self, tmp_path):
        repo, plan = make_repo(tmp_path)
        plan.write_text(GREENFIELD_PLAN)
        lines: list[str] = []
        result, _ = run_grouping(
            plan_path=plan,
            repo_root=repo,
            llm_runner=StubLlm(),
            client=make_client(repo),
            progress=lines.append,
        )
        total = len(result.groups)
        assert total >= 2  # otherwise "spec i/N" is not actually exercised
        stage_lines = [line for line in lines if line.startswith("stage:")]
        spec_lines = [line for line in lines if line.startswith("spec ")]
        assert stage_lines == [
            "stage: mapper",
            "stage: graph",
            "stage: partition",
            f"stage: specs total={total}",
        ]
        assert spec_lines == [f"spec {i}/{total}" for i in range(1, total + 1)]
        # Every progress line was emitted before the pipeline returned — the
        # unbuffered-in-the-CLI half of the fix is exercised by the CLI-level
        # test below; this half proves the seam actually fires per spec.
        assert lines.index(f"stage: specs total={total}") < lines.index(f"spec 1/{total}")

    def test_no_recorder_and_no_progress_still_works(self, tmp_path):
        """The seam is optional — omitting ``progress`` must not raise."""
        result, _ = grouping(tmp_path)
        assert result.groups
