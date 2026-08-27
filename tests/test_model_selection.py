"""Plan U17: three independently settable models — workers, the base session,
and the speccer/grouper — threaded to every place an argv is built.
"""

from __future__ import annotations

import functools
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.config import (
    DEFAULT_BASE_MODEL,
    DEFAULT_SPECCER_MODEL,
    DEFAULT_WORKER_MODEL,
    OrchestratorConfig,
    SessionConfig,
)
from orchestrator.execution.sessions import SessionRunner
from orchestrator.grouping.llm import claude_json_runner

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "fake-claude"
    (home / "sessions").mkdir(parents=True)
    return home


def make_runner(fake_home: Path, **kwargs) -> SessionRunner:
    env = {"FAKE_CLAUDE_HOME": str(fake_home), **kwargs.pop("env", {})}
    kwargs.setdefault("transcript_root", fake_home / "projects")
    return SessionRunner(claude_bin=[sys.executable, str(FAKE_CLAUDE)], env=env, **kwargs)


def calls(fake_home: Path) -> list[dict]:
    path = fake_home / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ------------------------------------------------------------- config defaults


def test_worker_model_defaults_to_sonnet():
    assert SessionConfig().model == DEFAULT_WORKER_MODEL
    assert "sonnet" in DEFAULT_WORKER_MODEL


def test_base_model_defaults_to_opus():
    assert SessionConfig().base_model == DEFAULT_BASE_MODEL
    assert "opus" in DEFAULT_BASE_MODEL


def test_speccer_model_defaults_to_opus():
    assert SessionConfig().speccer_model == DEFAULT_SPECCER_MODEL
    assert "opus" in DEFAULT_SPECCER_MODEL


def test_each_setting_is_readable_back_from_the_resolved_config():
    config = OrchestratorConfig()
    assert config.session.model == DEFAULT_WORKER_MODEL
    assert config.session.base_model == DEFAULT_BASE_MODEL
    assert config.session.speccer_model == DEFAULT_SPECCER_MODEL


def test_setting_the_worker_model_leaves_the_others_at_their_own_defaults():
    session = SessionConfig(model="claude-custom-worker")
    assert session.model == "claude-custom-worker"
    assert session.base_model == DEFAULT_BASE_MODEL
    assert session.speccer_model == DEFAULT_SPECCER_MODEL


def test_setting_the_base_model_leaves_the_others_at_their_own_defaults():
    session = SessionConfig(base_model="claude-custom-base")
    assert session.base_model == "claude-custom-base"
    assert session.model == DEFAULT_WORKER_MODEL
    assert session.speccer_model == DEFAULT_SPECCER_MODEL


def test_setting_the_speccer_model_leaves_the_others_at_their_own_defaults():
    session = SessionConfig(speccer_model="claude-custom-speccer")
    assert session.speccer_model == "claude-custom-speccer"
    assert session.model == DEFAULT_WORKER_MODEL
    assert session.base_model == DEFAULT_BASE_MODEL


# --------------------------------------------------------------- worker forks


def test_forked_worker_argv_carries_the_sonnet_model_with_no_configuration(fake_home, tmp_path):
    config = OrchestratorConfig()
    runner = make_runner(
        fake_home, model=config.session.model, base_model=config.session.base_model
    )
    base = runner.start_base(run_id="run1", base_context="ctx", cwd=tmp_path)
    runner.start_fork(
        base_id=base.session_id, prompt="do the task", name="run1-g1-coder", cwd=tmp_path
    )
    fork_call = [c for c in calls(fake_home) if "--fork-session" in c["argv"]][0]
    argv = fork_call["argv"]
    assert argv[argv.index("--model") + 1] == DEFAULT_WORKER_MODEL


def test_resume_argv_also_carries_the_worker_model(fake_home, tmp_path):
    runner = make_runner(fake_home, model=DEFAULT_WORKER_MODEL)
    result = runner.start_base(run_id="run1", base_context="ctx", cwd=tmp_path)
    runner.resume(session_id=result.session_id, prompt="continue", cwd=tmp_path)
    resume_call = [c for c in calls(fake_home) if "--resume" in c["argv"]][-1]
    argv = resume_call["argv"]
    assert argv[argv.index("--model") + 1] == DEFAULT_WORKER_MODEL


# ----------------------------------------------------------------- base session


def test_base_session_argv_carries_the_opus_model_with_no_configuration(fake_home, tmp_path):
    config = OrchestratorConfig()
    runner = make_runner(
        fake_home, model=config.session.model, base_model=config.session.base_model
    )
    runner.start_base(run_id="run1", base_context="ctx", cwd=tmp_path)
    (call,) = calls(fake_home)
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == DEFAULT_BASE_MODEL


def test_base_and_worker_models_are_independently_settable(fake_home, tmp_path):
    """The base session keeps Opus even when the worker model is overridden, and
    vice versa — each role's argv reflects only its own setting."""
    runner = make_runner(fake_home, model="claude-custom-worker", base_model=DEFAULT_BASE_MODEL)
    base = runner.start_base(run_id="run1", base_context="ctx", cwd=tmp_path)
    runner.start_fork(
        base_id=base.session_id, prompt="do the task", name="run1-g1-coder", cwd=tmp_path
    )
    base_call, fork_call = calls(fake_home)
    assert base_call["argv"][base_call["argv"].index("--model") + 1] == DEFAULT_BASE_MODEL
    assert fork_call["argv"][fork_call["argv"].index("--model") + 1] == "claude-custom-worker"


# ------------------------------------------------------------------- speccer


def test_grouper_argv_carries_the_opus_model_with_no_configuration(monkeypatch):
    recorded: dict = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": "{}"}), stderr="")

    monkeypatch.setattr("orchestrator.grouping.llm.subprocess.run", fake_run)
    config = OrchestratorConfig()
    runner = functools.partial(claude_json_runner, model=config.session.speccer_model)
    runner("prompt", {})
    argv = recorded["argv"]
    assert argv[argv.index("--model") + 1] == DEFAULT_SPECCER_MODEL


def test_grouper_argv_omits_model_flag_when_unset(monkeypatch):
    recorded: dict = {}

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": "{}"}), stderr="")

    monkeypatch.setattr("orchestrator.grouping.llm.subprocess.run", fake_run)
    claude_json_runner("prompt", {})
    assert "--model" not in recorded["argv"]
