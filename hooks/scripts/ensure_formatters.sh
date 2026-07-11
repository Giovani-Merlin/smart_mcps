#!/bin/sh
# SessionStart hook: best-effort install of the formatters used by
# lint_after_edit.py so its fast PATH branch is populated.
# Idempotent, detached, never fails the session.
command -v uv >/dev/null 2>&1 || exit 0
(
  uv tool install ruff >/dev/null 2>&1
  # plugins are mandatory: plain mdformat corrupts YAML frontmatter
  uv tool install mdformat --with mdformat-gfm --with mdformat-frontmatter >/dev/null 2>&1
) &
exit 0
