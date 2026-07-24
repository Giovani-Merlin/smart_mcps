"""U9 (R26): opt-in LLM-in-the-loop scenarios — the two paths the deterministic
fixtures (test_grouping_fixtures.py) cannot cover because they require a real
model call. Excluded from the default run via the `llm` marker (pyproject.toml
addopts); run explicitly with `uv run pytest -m llm`. Codegraph stays stubbed
(files-only) in both scenarios — only the LLM call itself is real.
"""

from pathlib import Path

import pytest

from orchestrator.grouping.graphing import CodegraphClient
from orchestrator.grouping.llm import claude_json_runner
from orchestrator.grouping.pipeline import compute_partition, run_grouping

pytestmark = pytest.mark.llm

# Reuses the pure-backend fixture (deterministic, cycle-free, two groups) so this
# scenario doesn't need its own hand-rolled task-map plan.
TASK_MAP_PLAN = (Path(__file__).parent / "fixtures" / "grouping" / "pure-backend.md").read_text()

GREENFIELD_NO_TASK_MAP_PLAN = """# feat: checkout service

## Tasks

- T1: build the checkout API in `app/checkout.py`
- T2: build the checkout admin page in `web/checkout.tsx`
"""


def stub_codegraph_runner(args):
    if args[0] == "files":
        return "stub repo (llm test — codegraph itself stays stubbed)\n"
    raise AssertionError(f"unexpected codegraph call: {args}")


def client_for(repo):
    return CodegraphClient(repo_root=repo, runner=stub_codegraph_runner)


def make_repo(tmp_path, plan_text):
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = repo / "plan.md"
    plan.write_text(plan_text)
    return repo, plan


class TestTaskMapFastPathEndToEnd:
    """Scenario 1: a task-map plan skips the mapper LLM entirely — the only
    real call this makes is the speccer's, once."""

    def test_mapper_skipped_flag_and_every_group_gets_a_nonempty_spec(self, tmp_path):
        repo, plan = make_repo(tmp_path, TASK_MAP_PLAN)
        result, _ = run_grouping(
            plan_path=plan,
            repo_root=repo,
            llm_runner=claude_json_runner,
            client=client_for(repo),
        )
        assert result.flags[0] == "task map: parsed from plan — mapper LLM skipped"
        assert result.groups
        for group in result.groups:
            assert group.spec.strip()


class TestMapperFallbackOnGreenfieldPlan:
    """Scenario 2: a plan with no task map runs the real mapper LLM. The
    partition-only path (compute_partition) is enough here — it stops before
    the speccer, so this makes exactly one real call, same as scenario 1."""

    def test_nonexistent_mapped_files_are_dropped_and_flagged_not_retained(self, tmp_path):
        repo, plan = make_repo(tmp_path, GREENFIELD_NO_TASK_MAP_PLAN)
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=claude_json_runner,
            client=client_for(repo),
        )
        assert any(
            "mapped nonexistent file" in flag and "dropped" in flag
            for flag in outcome.mapper_out.flags
        )
        for mapping in outcome.mapper_out.mappings:
            assert mapping.prospective_files == ()
            for file in mapping.files:
                assert (repo / file).is_file()
