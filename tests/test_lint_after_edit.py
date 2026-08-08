"""Tests for hooks/scripts/lint_after_edit.py."""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "hooks" / "scripts" / "lint_after_edit.py"

spec = importlib.util.spec_from_file_location("lint_after_edit", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


@pytest.fixture(autouse=True)
def _reset_warned():
    mod._warned.clear()


@pytest.fixture
def run_recorder(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return calls


def _which_map(available: dict[str, str]):
    return lambda tool: available.get(tool)


def test_py_edit_runs_ruff_check_then_format(tmp_path, monkeypatch, run_recorder):
    py = tmp_path / "bad.py"
    py.write_text("x=1\n")
    monkeypatch.setattr(mod.shutil, "which", _which_map({"ruff": "/usr/bin/ruff"}))

    mod._lint(str(py))

    assert run_recorder == [
        ["/usr/bin/ruff", "check", "--fix", "--quiet", str(py)],
        ["/usr/bin/ruff", "format", "--quiet", str(py)],
    ]


def test_md_edit_uses_uvx_with_plugins(tmp_path, monkeypatch, run_recorder):
    md = tmp_path / "doc.md"
    md.write_text("# hi\n")
    monkeypatch.setattr(mod.shutil, "which", _which_map({"uvx": "/usr/bin/uvx"}))

    mod._lint(str(md))

    assert run_recorder == [
        [
            "uvx",
            "--with",
            "mdformat-gfm",
            "--with",
            "mdformat-frontmatter",
            "mdformat",
            str(md),
        ]
    ]


def test_resolver_prefers_path_binary(monkeypatch):
    monkeypatch.setattr(
        mod.shutil, "which", _which_map({"ruff": "/usr/bin/ruff", "uvx": "/usr/bin/uvx"})
    )
    assert mod._resolve("ruff") == ["/usr/bin/ruff"]


def test_resolver_falls_back_to_uvx(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", _which_map({"uvx": "/usr/bin/uvx"}))
    assert mod._resolve("ruff") == ["uvx", "ruff"]


def test_resolver_warns_once_when_nothing_available(monkeypatch, capsys):
    monkeypatch.setattr(mod.shutil, "which", _which_map({}))
    assert mod._resolve("ruff") is None
    assert mod._resolve("ruff") is None
    err = capsys.readouterr().err
    assert err.count("ruff") == 1


def test_missing_tools_leave_file_untouched(tmp_path, monkeypatch, run_recorder):
    py = tmp_path / "bad.py"
    original = "x=1\n"
    py.write_text(original)
    monkeypatch.setattr(mod.shutil, "which", _which_map({}))

    mod._lint(str(py))

    assert run_recorder == []
    assert py.read_text() == original


def test_multiedit_formats_all_paths(tmp_path, monkeypatch, run_recorder):
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x=1\n")
    b.write_text("y=2\n")
    monkeypatch.setattr(mod.shutil, "which", _which_map({"ruff": "/usr/bin/ruff"}))
    payload = {
        "tool_name": "MultiEdit",
        "tool_input": {"edits": [{"file_path": str(a)}, {"file_path": str(b)}]},
    }
    monkeypatch.setattr(
        mod.sys, "stdin", type("S", (), {"read": staticmethod(lambda: json.dumps(payload))})()
    )

    mod.main()

    touched = {c[-1] for c in run_recorder}
    assert touched == {str(a), str(b)}


def test_other_extensions_ignored(tmp_path, monkeypatch, run_recorder):
    txt = tmp_path / "notes.txt"
    txt.write_text("hello\n")
    monkeypatch.setattr(mod.shutil, "which", _which_map({"ruff": "/usr/bin/ruff"}))

    mod._lint(str(txt))

    assert run_recorder == []


def test_nonexistent_path_ignored(tmp_path, monkeypatch, run_recorder):
    mod._lint(str(tmp_path / "ghost.py"))
    assert run_recorder == []


def test_malformed_json_exits_zero():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], input="not json", capture_output=True, text=True
    )
    assert result.returncode == 0


@pytest.mark.skipif(shutil.which("uvx") is None, reason="uvx not available")
def test_frontmatter_survives_real_mdformat(tmp_path):
    """Integration: the hook must not corrupt YAML frontmatter in skill files."""
    source = REPO_ROOT / "skills" / "codegraph" / "REFERENCE_SKILL.md"
    doc = tmp_path / "SKILL.md"
    doc.write_text(source.read_text())
    frontmatter_before = doc.read_text().split("---")[1]

    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(doc)}}
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], input=json.dumps(payload), capture_output=True, text=True
    )

    assert result.returncode == 0
    text_after = doc.read_text()
    assert text_after.startswith("---")
    assert text_after.split("---")[1] == frontmatter_before
