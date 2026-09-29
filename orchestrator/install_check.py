"""Refuse to launch a run on a stale ``uv tool`` install of the orchestrator.

``smart-mcps-orchestrate`` is installed with ``uv tool install <repo>``, which
copies the package — it is not editable. A copy older than its source silently
runs old orchestrator code: r20260923-163956 ran on a two-week-old build with
no driver-run gating, and r20260927-100604's first launch died on argparse
because the copy predated ``--plan``. The check that looks right is wrong
(``python -c 'import orchestrator'`` from the repo imports the repo copy, since
cwd is on ``sys.path``), so this compares the *running* package against the
source directory the tool's own ``uv-receipt.toml`` names.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import orchestrator

#: How many differing files the refusal names before summarising.
_SHOWN = 5


def _receipt_source(prefix: Path) -> Path | None:
    """The source directory a uv tool environment was installed from, or
    ``None`` when this interpreter is not a uv tool install from a directory."""
    receipt = prefix / "uv-receipt.toml"
    try:
        data = tomllib.loads(receipt.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    for requirement in data.get("tool", {}).get("requirements", []):
        directory = requirement.get("directory") if isinstance(requirement, dict) else None
        if directory:
            return Path(directory)
    return None


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def stale_install_differences(
    installed: Path | None = None, prefix: Path | None = None
) -> tuple[Path, list[str]] | None:
    """``(source_dir, differing relative paths)`` when the running package is a
    uv tool copy whose source has moved on; ``None`` when it is current, not a
    uv tool install, or its source is gone (nothing to compare against)."""
    installed = (installed or Path(orchestrator.__file__).parent).resolve()
    source = _receipt_source(prefix or Path(sys.prefix))
    if source is None:
        return None
    source_pkg = (source / "orchestrator").resolve()
    if not source_pkg.is_dir() or installed.is_relative_to(source_pkg):
        return None  # source gone, or an editable/in-repo import
    ours, theirs = _files(installed), _files(source_pkg)
    differing = sorted(p for p in ours.keys() | theirs.keys() if ours.get(p) != theirs.get(p))
    return (source, differing) if differing else None


def stale_install_message() -> str | None:
    """The refusal text for a stale install, or ``None`` when it is current."""
    found = stale_install_differences()
    if found is None:
        return None
    source, differing = found
    shown = ", ".join(differing[:_SHOWN])
    more = f" and {len(differing) - _SHOWN} more" if len(differing) > _SHOWN else ""
    return (
        f"the installed orchestrator differs from its source {source} in "
        f"{len(differing)} file(s) ({shown}{more}) — this run would execute old code. "
        f"Reinstall with `uv tool install --reinstall {source}`, or pass "
        "--allow-stale-install to launch anyway"
    )
