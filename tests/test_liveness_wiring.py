"""The liveness plumbing is wired into a real run, not only unit-tested.

Plans U1/U2/U5 shipped `ActivityRegistry`, `LivenessProbe` and the Suspend Cure
seams with green tests against directly-constructed objects, while nothing in
`_cmd_run` ever built a registry or installed a probe — the same "mechanism
tested, wiring absent" shape that once left Landlock dead for a release (see
`test_confinement.py`). These tests assert the construction sites.
"""

from __future__ import annotations

import inspect
from dataclasses import replace

from orchestrator import cli
from orchestrator.cli import build_session_runner
from orchestrator.config import LivenessConfig, OrchestratorConfig
from orchestrator.execution.liveness import ActivityRegistry
from orchestrator.execution.review import _GroupExecution
from orchestrator.model import ReviewIntensity
from tests.test_review_loop import Harness, StubRunner, make_group


def test_build_session_runner_hands_the_registry_to_the_runner():
    registry = ActivityRegistry()
    runner = build_session_runner(OrchestratorConfig(), activity=registry)
    assert runner.activity is registry


def test_cmd_run_shares_one_registry_between_runner_and_review_deps():
    source = inspect.getsource(cli)
    assert "activity = ActivityRegistry()" in source
    assert "activity=activity," in source
    assert "liveness=config.liveness," in source


def test_group_execution_installs_a_probe_reading_its_own_worktree(tmp_path):
    harness = Harness(tmp_path, StubRunner({}))
    registry = ActivityRegistry()
    deps = replace(harness.deps, activity=registry, liveness=LivenessConfig())
    group = make_group(intensity=ReviewIntensity.SELF_VERIFY)
    execution = _GroupExecution(deps, harness.context(group))

    assert len(execution._heartbeat._tick_hooks) == 1
    assert execution._heartbeat.activity_provider is not None

    execution.workspace = harness.workspace
    registry.spawned(4242, session_id="s-1", cwd=str(harness.workspace))
    registry.spawned(4343, session_id="s-other", cwd=str(tmp_path / "elsewhere"))
    child = execution._current_child()
    assert child is not None and child.session_id == "s-1"


def test_group_execution_without_liveness_installs_nothing(tmp_path):
    harness = Harness(tmp_path, StubRunner({}))
    execution = _GroupExecution(harness.deps, harness.context(make_group()))
    assert execution._heartbeat._tick_hooks == []
    assert execution._current_child() is None
