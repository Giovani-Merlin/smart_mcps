# smart-mcps

A Claude Code plugin that installs skills and session hooks for codegraph, NotebookLM, and Perplexity.

## Plugin structure

This is a **Claude Code plugin repository**. Hooks and skills are distributed to plugin consumers via auto-discovered files:

| Path                         | Purpose                                                                              |
| ---------------------------- | ------------------------------------------------------------------------------------ |
| `hooks/hooks.json`           | Plugin-level hook registrations (uses `${CLAUDE_PLUGIN_ROOT}`)                       |
| `hooks/scripts/`             | Hook implementation scripts (`.py` and `.sh`)                                        |
| `skills/`                    | Skill definitions                                                                    |
| `agents/`                    | Subagent definitions (auto-discovered; `.claude/agents` symlinks here for local dev) |
| `codegraph_mcp/`             | FastMCP proxy exposing 6 trimmed codegraph tools (`smart-mcps-codegraph`)            |
| `.mcp.json`                  | MCP server registration — serves plugin consumers **and** local dev (see below)      |
| `.claude-plugin/plugin.json` | Plugin identity (name, version)                                                      |
| `.claude/settings.json`      | Project-level hooks for local development (uses `$CLAUDE_PROJECT_DIR`)               |

### Adding hooks — always register in both places

Every hook script must be wired up in **two** files:

1. **`hooks/hooks.json`** — so plugin consumers receive it. Uses `${CLAUDE_PLUGIN_ROOT}`.
2. **`.claude/settings.json`** — so it runs locally during development. Uses `$CLAUDE_PROJECT_DIR`.

Matchers are **case-sensitive** and PascalCase in **both** files (e.g. `"Edit|Write|MultiEdit"`, `"Bash"`) — a lowercase matcher silently matches nothing. This applies to every event, including `SessionStart` (codegraph reindex, formatter bootstrap), not just `PostToolUse`.

Registering in only one place means either plugin consumers or local dev is broken. Always do both.

### MCP servers — one file, not two

The dual-registration rule above does **not** apply to MCP servers: `.claude/settings.json` has no `mcpServers` key (it only *approves* servers via `enabledMcpjsonServers`). A single `.mcp.json` at the repo root covers both audiences, because Claude Code reads it twice:

- **Plugin consumers** — auto-discovered as the plugin's `./.mcp.json`; tools resolve as `mcp__plugin_smart-mcps_codegraph__<tool>`.
- **Local dev** — read as the project-scoped `.mcp.json`; tools resolve as `mcp__codegraph__<tool>`.

Hence `"--project", "${CLAUDE_PLUGIN_ROOT:-.}"`. `${CLAUDE_PLUGIN_ROOT}` is set only for consumers; locally it falls back to `.` (the server's cwd is the project root). Two constraints worth knowing before editing that line:

- **`${CLAUDE_PROJECT_DIR}` is not available in `.mcp.json`** — only in hooks. Using it yields a "Missing environment variables" warning.
- **Defaults don't nest.** `${VAR:-default}` works, but `${A:-${B}}` does not expand `B` — it silently passes a literal, which `uv` may accept when the console script is already on PATH, hiding the bug locally while breaking for consumers.
