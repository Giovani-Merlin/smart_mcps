"""What a `Bash(...)` permission rule actually matches, per the **real** CLI.

`DEFAULT_ALLOWED_TOOLS` is only as good as its guess about the matcher, and the
guess has now been wrong twice in production. First a missing tool (`npm`, which
killed g8 three rounds running). Then a missing *spelling* of a tool already
allowed: group g2 of run r20260812-202855 died on

    .venv/bin/python -m pytest tests/test_devices.py -q

with `Bash(python *)` sitting in the allowlist the whole time. One program, many
names; a rule matches the command as written.

The obvious repair — a leading wildcard, `Bash(*python *)` — **does not work**,
and nothing in the codebase could have told us that. Only the binary knows. These
tests are that knowledge, written down and re-checkable: they probe the live CLI
for which patterns grant a path-qualified command and which silently do not.

They spend real tokens; opt in with `-m llm`.

Mechanics worth keeping (each cost a wrong result before it was noticed):

- The prompt goes on **stdin**. `--allowedTools` is variadic (`<tools...>`), so a
  prompt passed as a trailing argument is swallowed as another tool name and the
  CLI dies with "Input must be provided...".
- `--setting-sources ''` excludes the operator's own `~/.claude/settings.json`.
  Without it the probe is meaningless: `--allowedTools` *adds* to the operator's
  rules rather than replacing them (see `test_e2e_live.py`), so an operator rule
  can grant the command and make any pattern look like it worked.
- A control case that must be **denied** is what proves the probe can still fail.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from orchestrator.config import DEFAULT_ALLOWED_TOOLS

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH"),
]

MARKER = "PROBE_RAN_42"
PROMPT = (
    'Run the shell command `.venv/bin/python -c "print(42)"` using the Bash tool. Do not explain.'
)
PROBE_TIMEOUT_S = 120


@pytest.fixture(scope="module")
def probe_dir(tmp_path_factory) -> Path:
    """A scratch repo holding a stub `.venv/bin/python` that announces itself.

    A stub, not the real interpreter: the question is whether the *permission
    layer* let the command run, and a program that prints one unmistakable token
    answers that without depending on any interpreter behaviour.
    """
    root = tmp_path_factory.mktemp("permprobe")
    venv_python = root / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text(f"#!/bin/sh\necho {MARKER}\n")
    venv_python.chmod(0o755)
    return root


def _command_ran(pattern: str, cwd: Path) -> bool:
    """True when the rule let `.venv/bin/python` actually execute."""
    proc = subprocess.run(
        [
            "claude",
            "--print",
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
            "--setting-sources",
            "",
            "--allowedTools",
            pattern,
        ],
        input=PROMPT,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=PROBE_TIMEOUT_S,
        env=dict(os.environ),
    )
    return MARKER in proc.stdout


def test_a_rule_granting_nothing_relevant_is_denied(probe_dir: Path) -> None:
    """The control. If this ever passes, every other assertion here is vacuous —
    something outside the flag is granting Bash and the probe proves nothing."""
    assert not _command_ran("Read", probe_dir), (
        "a probe with no Bash rule still ran the command; the isolation "
        "(--setting-sources '') has stopped working and these results are void"
    )


def test_a_bare_name_rule_does_not_match_the_same_tool_invoked_by_path(
    probe_dir: Path,
) -> None:
    """`Bash(python *)` vs `.venv/bin/python …` — the g2 failure, reproduced."""
    assert not _command_ran("Bash(python *)", probe_dir)


def test_a_leading_wildcard_alone_does_not_match(probe_dir: Path) -> None:
    """The intuitive fix, and it is wrong.

    Pinned deliberately: `Bash(*python *)` reads as "python anywhere" and would be
    a reasonable thing for a future maintainer to reach for. It does not work, and
    the failure is silent — the group just dies on a denial. If a CLI change ever
    makes it work, this test fails and the baseline can be simplified on purpose.
    """
    assert not _command_ran("Bash(*python *)", probe_dir)


def test_a_slash_aligned_wildcard_matches_any_path_prefix(probe_dir: Path) -> None:
    """The form `DEFAULT_ALLOWED_TOOLS` ships. The wildcard must align to a `/`."""
    assert _command_ran("Bash(*/python *)", probe_dir), (
        "the */ form stopped matching path-qualified commands — "
        "DEFAULT_ALLOWED_TOOLS' path-qualified twins are now dead weight"
    )


def test_the_shipped_baseline_covers_a_venv_interpreter() -> None:
    """The end the probing was for, asserted against the real tuple.

    Cheap and offline, but it is the rule that was missing when g2 died, so it
    belongs next to the evidence for why it has this shape.
    """
    assert "Bash(*/python *)" in DEFAULT_ALLOWED_TOOLS
    assert "Bash(*/npm *)" in DEFAULT_ALLOWED_TOOLS
    # W1 (r20260828-090936): g4 had to substitute HTTP smoke tests because
    # curl, pkill and the agent-browser CLI were all denied — ordinary
    # verification tooling for any group that must prove a service responds.
    assert "Bash(curl *)" in DEFAULT_ALLOWED_TOOLS
    assert "Bash(pkill *)" in DEFAULT_ALLOWED_TOOLS
    assert "Bash(agent-browser *)" in DEFAULT_ALLOWED_TOOLS
    assert "Bash(*/curl *)" in DEFAULT_ALLOWED_TOOLS  # the auto-generated twin
    # Every executable rule got a twin; the argument-less one did not need one.
    assert "Bash(*/env)" not in DEFAULT_ALLOWED_TOOLS
    assert "Bash(*)" not in DEFAULT_ALLOWED_TOOLS, "the baseline must not grant every command"
