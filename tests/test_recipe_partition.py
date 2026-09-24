"""Tests for plan U4 — single-recipe groups: the partitioner isolates every
non-``code`` unit as its own group and refuses to mix recipes.

Written against the same pipeline seam as ``test_grouper_pipeline.py``: a
task-mapped plan needs no LLM mapper, so every fixture embeds an
``orchestrator-task-map v2`` block and drives ``compute_partition`` /
``run_grouping`` / the CLI directly. Task ids here deliberately do NOT use the
``uN-...`` convention (``plan_sections.unit_key_for_task``), so no ``## Units``
heading section is required for any of these plans.
"""

from __future__ import annotations

import json

from orchestrator.config import OrchestratorConfig
from orchestrator.grouping.graphing import CodegraphClient
from orchestrator.grouping.pipeline import GrouperError, compute_partition, run_grouping
from orchestrator.model import ReviewIntensity


def run_enabled_config() -> OrchestratorConfig:
    return OrchestratorConfig(recipes={"enabled": ["run"]})


def codegraph_response(args):
    """Every fixture below declares no ``symbols:``, so the pipeline never
    calls callers/callees/impact for a real symbol — ``query`` (including the
    logical-export/quiescence "list everything" call) always answers empty."""
    command = args[0]
    if command == "sync":
        return ""
    if command == "files":
        return "repo files"
    if command == "status":
        return json.dumps(
            {
                "initialized": True,
                "fileCount": 1,
                "nodeCount": 1,
                "edgeCount": 0,
                "pendingChanges": {"added": 0, "modified": 0, "removed": 0},
            }
        )
    if command == "query":
        return json.dumps([])
    raise AssertionError(f"unexpected codegraph command: {args}")


def make_client(repo):
    return CodegraphClient(repo_root=repo, runner=codegraph_response)


def _llm_must_not_be_called(prompt, schema):
    raise AssertionError("task-mapped plans never call the LLM mapper")


RUN_COMMANDS = """      commands:
        - cmd: "echo hi"
          wall_clock_min: 5
"""

# g3-2: code a -> b, a -> c; run r depends on b; code d depends on r.
FIXTURE_G3_2 = f"""# feat: build-graph shape

## Task Map

```yaml
# orchestrator-task-map v2
tasks:
  - task_id: a
    description: task a
    files: [app/a.py]
  - task_id: b
    description: task b
    files: [app/b.py]
    depends_on: [a]
  - task_id: c
    description: task c
    files: [app/c.py]
    depends_on: [a]
  - task_id: r
    description: run task r
    recipe: run
    recipe_args:
{RUN_COMMANDS}    depends_on: [b]
  - task_id: d
    description: task d
    files: [app/d.py]
    depends_on: [r]
```
"""


# g3-3 / g3-4: build (u1) -> run (r) -> tune (u3), u1/u3 share a file (would
# otherwise cluster) — g3-4 additionally puts u1/u3 in one declared slice.
def _build_run_tune_plan(shared_slice: bool) -> str:
    slice_lines = "    slice: shared\n" if shared_slice else ""
    return f"""# feat: build-run-tune shape

## Task Map

```yaml
# orchestrator-task-map v2
tasks:
  - task_id: u1
    description: build
    files: [shared.py]
{slice_lines}  - task_id: r
    description: run
    recipe: run
    recipe_args:
{RUN_COMMANDS}    depends_on: [u1]
  - task_id: u3
    description: tune
    files: [shared.py]
    depends_on: [r]
{slice_lines}```
"""


FIXTURE_G3_3 = _build_run_tune_plan(shared_slice=False)
FIXTURE_G3_4 = _build_run_tune_plan(shared_slice=True)

# g3-6: a v2 fixture whose code units are identical to a hub-shaped v1
# fixture's, plus one run unit depending on all of them.
HUB_CODE_TASKS = """  - task_id: base
    description: base
    files: [app/base.py]
  - task_id: leaf-a
    description: leaf a
    files: [app/leaf_a.py]
    depends_on: [base]
  - task_id: leaf-b
    description: leaf b
    files: [app/leaf_b.py]
    depends_on: [base]
  - task_id: leaf-c
    description: leaf c
    files: [app/leaf_c.py]
    depends_on: [base]
"""

FIXTURE_HUB_CODE_ONLY = f"""# feat: hub shape, code only

## Task Map

```yaml
# orchestrator-task-map v2
tasks:
{HUB_CODE_TASKS}```
"""

FIXTURE_HUB_WITH_RUN = f"""# feat: hub shape plus a run unit

## Task Map

```yaml
# orchestrator-task-map v2
tasks:
{HUB_CODE_TASKS}  - task_id: r
    description: run everything
    recipe: run
    recipe_args:
{RUN_COMMANDS}    depends_on: [base, leaf-a, leaf-b, leaf-c]
```
"""


def make_repo(tmp_path, plan_text, name="repo"):
    repo = tmp_path / name
    repo.mkdir(exist_ok=True)
    (repo / "shared.py").write_bytes(b"def shared():\n    pass\n" * 5)
    plan = repo / "plan.md"
    plan.write_text(plan_text)
    return repo, plan


class TestBuildGraphShape:
    """g3-2: the run unit is isolated, priced, self-verified, and ordered
    between its upstream and downstream code groups."""

    def test_run_unit_is_isolated_priced_and_ordered(self, tmp_path):
        repo, plan = make_repo(tmp_path, FIXTURE_G3_2)
        result, _ = run_grouping(
            plan_path=plan,
            repo_root=repo,
            config=run_enabled_config(),
            llm_runner=_llm_must_not_be_called,
            client=make_client(repo),
        )
        by_task = {task: group for group in result.groups for task in group.tasks}
        r_group = by_task["r"]
        b_group = by_task["b"]
        d_group = by_task["d"]

        assert r_group.tasks == ["r"]
        assert r_group.recipe == "run"
        assert r_group.intensity == ReviewIntensity.SELF_VERIFY
        assert r_group.estimated_wall_clock_s == 5 * 60

        assert r_group.id in d_group.dependencies
        assert b_group.id in r_group.dependencies


class TestBuildRunTuneSplit:
    """g3-3: a code group that both feeds and consumes the same run unit is
    split at that boundary, not merged with it."""

    def test_split_orders_build_run_tune_and_flags_it(self, tmp_path):
        repo, plan = make_repo(tmp_path, FIXTURE_G3_3)
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            config=run_enabled_config(),
            llm_runner=_llm_must_not_be_called,
            client=make_client(repo),
        )
        u1_gid = outcome.partition["u1"]
        r_gid = outcome.partition["r"]
        u3_gid = outcome.partition["u3"]

        assert len({u1_gid, r_gid, u3_gid}) == 3
        assert outcome.dag.get(u1_gid, set()) >= {r_gid}
        assert outcome.dag.get(r_gid, set()) >= {u3_gid}
        assert any("split" in flag and "r" in flag for flag in outcome.flags)


class TestBuildRunTuneUnsplittable:
    """g3-4: the same shape, but u1/u3 share a declared slice — unsplittable."""

    def test_shared_slice_raises_naming_the_singleton_and_both_tasks(self, tmp_path):
        repo, plan = make_repo(tmp_path, FIXTURE_G3_4)
        try:
            compute_partition(
                plan_path=plan,
                repo_root=repo,
                config=run_enabled_config(),
                llm_runner=_llm_must_not_be_called,
                client=make_client(repo),
            )
        except GrouperError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected GrouperError")
        assert "r" in message
        assert "u1" in message
        assert "u3" in message


class TestRecipeGate:
    """g3-5: `[recipes] enabled = []` fails `group` naming the run unit and
    the config key, at the CLI."""

    def test_disabled_recipe_fails_naming_unit_and_key(self, tmp_path, capsys):
        from orchestrator.cli import main

        repo, plan = make_repo(tmp_path, FIXTURE_G3_2)
        config_dir = repo / ".orchestrator"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("[recipes]\nenabled = []\n")
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            llm_runner=_llm_must_not_be_called,
            client=make_client(repo),
        )
        assert exit_code != 0
        err = capsys.readouterr().err
        assert "r" in err
        assert "[recipes] enabled" in err


class TestHubRolesUnaffectedByRunUnit:
    """g3-6: adding a run unit that depends on every code task must not move
    any code task's hub role."""

    def test_hub_roles_identical_with_and_without_the_run_unit(self, tmp_path):
        repo_a, plan_a = make_repo(tmp_path, FIXTURE_HUB_CODE_ONLY, name="repo-a")
        outcome_a = compute_partition(
            plan_path=plan_a,
            repo_root=repo_a,
            llm_runner=_llm_must_not_be_called,
            client=make_client(repo_a),
        )

        repo_b, plan_b = make_repo(tmp_path, FIXTURE_HUB_WITH_RUN, name="repo-b")
        outcome_b = compute_partition(
            plan_path=plan_b,
            repo_root=repo_b,
            config=run_enabled_config(),
            llm_runner=_llm_must_not_be_called,
            client=make_client(repo_b),
        )

        code_tasks = {"base", "leaf-a", "leaf-b", "leaf-c"}
        assert outcome_a.hub_roles["base"] == "utility_hub"
        for task in code_tasks:
            assert outcome_a.hub_roles[task] == outcome_b.hub_roles[task]
