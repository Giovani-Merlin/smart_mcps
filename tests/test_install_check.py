"""`run`/`resume` refuse to launch on a stale uv-tool install.

Regression for run r20260927-100604, whose first launch died on argparse
because the installed copy predated ``--plan`` (and r20260923-163956, which ran
a two-week-old build to completion without anyone noticing).
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.install_check import stale_install_differences


def _tool_env(tmp_path: Path, source: Path) -> Path:
    prefix = tmp_path / "tool-env"
    prefix.mkdir()
    (prefix / "uv-receipt.toml").write_text(
        f'[tool]\nrequirements = [{{ name = "smart-mcps", directory = "{source}" }}]\n'
    )
    return prefix


def _package(root: Path, files: dict[str, str]) -> Path:
    pkg = root / "orchestrator"
    for rel, text in files.items():
        (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
        (pkg / rel).write_text(text)
    return pkg


def test_a_copy_behind_its_source_names_the_differing_files(tmp_path):
    source = tmp_path / "repo"
    _package(source, {"cli.py": "new\n", "prompts/x.md": "same\n", "added.py": "\n"})
    installed = _package(tmp_path / "site", {"cli.py": "old\n", "prompts/x.md": "same\n"})
    (installed / "__pycache__").mkdir()
    (installed / "__pycache__" / "cli.cpython-312.pyc").write_bytes(b"\0")

    found = stale_install_differences(installed, _tool_env(tmp_path, source))

    assert found is not None
    assert found[0] == source
    assert found[1] == ["added.py", "cli.py"]


def test_a_current_copy_passes(tmp_path):
    files = {"cli.py": "x\n", "prompts/x.md": "y\n"}
    source = tmp_path / "repo"
    _package(source, files)
    installed = _package(tmp_path / "site", files)
    assert stale_install_differences(installed, _tool_env(tmp_path, source)) is None


def test_not_a_uv_tool_install_or_an_in_repo_import_passes(tmp_path):
    source = tmp_path / "repo"
    pkg = _package(source, {"cli.py": "x\n"})
    no_receipt = tmp_path / "plain-venv"
    no_receipt.mkdir()
    assert stale_install_differences(pkg, no_receipt) is None
    # The running package *is* the source (editable install, `uv run` in the repo).
    assert stale_install_differences(pkg, _tool_env(tmp_path, source)) is None
