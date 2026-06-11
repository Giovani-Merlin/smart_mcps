# smart-mcps

A Claude Code plugin that installs skills and session hooks for agentmemory, codegraph, NotebookLM, and Perplexity.

## Plugin structure

This is a **Claude Code plugin repository**. Hooks and skills are distributed to plugin consumers via auto-discovered files:

| Path | Purpose |
|------|---------|
| `hooks/hooks.json` | Plugin-level hook registrations (uses `${CLAUDE_PLUGIN_ROOT}`) |
| `hooks/scripts/` | Hook implementation scripts (`.mjs` and `.py`) |
| `skills/` | Skill definitions |
| `agents/` | Subagent definitions (auto-discovered; `.claude/agents` symlinks here for local dev) |
| `.claude-plugin/plugin.json` | Plugin identity (name, version) |
| `.claude/settings.json` | Project-level hooks for local development (uses `$CLAUDE_PROJECT_DIR`) |

### Adding hooks — always register in both places

Every hook script must be wired up in **two** files:

1. **`hooks/hooks.json`** — so plugin consumers receive it. Uses `${CLAUDE_PLUGIN_ROOT}`. Matchers use PascalCase: `"Edit|Write|MultiEdit"`.
2. **`.claude/settings.json`** — so it runs locally during development. Uses `$CLAUDE_PROJECT_DIR`. Matchers use lowercase: `"edit|write|multiedit"`.

Registering in only one place means either plugin consumers or local dev is broken. Always do both.

## Code exploration

Prioritize `codegraph context "<query>"` for exploring code — see the codegraph skill (`skills/codegraph/SKILL.md`). Use it first for any code-structure question (where is X defined, what calls Y, what breaks if Z changes) before falling back to `grep`/`find`/`Read`.
