# smart-mcps

A Claude Code plugin that installs **skills** and **session hooks** for codegraph, NotebookLM, and Perplexity. NotebookLM and Perplexity run as CLI commands — no MCP servers, no context-window pollution.

**Codegraph is the one deliberate exception.** Code exploration is the one case where the tool has to be in the model's tool list at the moment it decides how to explore — a skill it must first *think* to reach for loses to Grep every time. MCP is the only mechanism that puts a tool there, so the plugin ships a trimmed FastMCP proxy: 6 tools for ~1,000 tokens, against ~2,400 for upstream's 10. The remaining `codegraph` CLI commands stay reachable via Bash.

## Installation

Clone the repo into your project (or anywhere you want the skills to live) so you can edit them:

```bash
git clone https://github.com/Giovani-Merlin/smart_mcps
```

Then install the plugin in Claude Code:

```text
/plugin marketplace add https://github.com/Giovani-Merlin/smart_mcps
/plugin install smart-mcps
```

Skills are registered automatically on install. Restart Claude Code after installation.

> **Skills are meant to be edited.** After installing, configure each skill you use — see [Setup](#setup-per-tool) below. The `notebooklm-chat` skill in particular ships with a placeholder notebook map that you must fill in.

______________________________________________________________________

## Skills

| Skill                 | Trigger                | What it does                                                                                                                     |
| --------------------- | ---------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `codegraph`           | `/codegraph`           | Symbol lookup, call graph tracing, impact analysis via the `codegraph` MCP tools                                                 |
| `notebooklm-chat`     | `/notebooklm-chat`     | Chat with a notebook by topic — query only, **requires configuring your notebook map** in the skill file                         |
| `notebooklm-complete` | `/notebooklm-complete` | Full NotebookLM management: query, create, add sources, audio/video artifacts                                                    |
| `perplexity`          | `/perplexity`          | Web-grounded search, research, and reasoning via `smart-mcps-perplexity` CLI                                                     |
| `plan-to-plan`        | `/plan-to-plan`        | Research planning — decomposes a topic into tagged external-knowledge questions and writes an approved `research_plan.md`        |
| `apply-research-plan` | `/apply-research-plan` | Executes an approved `research_plan.md` via subagents, then writes `research_answers.md` and a concrete `implementation_plan.md` |
| `plan-notebookllm`    | `/plan-notebookllm`    | Notebook-grounded planning — queries a NotebookLM notebook before producing a plan                                               |

______________________________________________________________________

## Setup (per tool)

### Codegraph

Codegraph is an **npm** package — not PyPI. (`pip install codegraph` installs an unrelated project of the same name.) Run once per project:

```bash
npm install -g @colbymchenry/codegraph
codegraph init
codegraph index
```

The SessionStart hook runs `codegraph index --force` (detached, prunes deleted files) automatically on each session start if `codegraph` is installed, initializing the index first if needed.

#### MCP tools and permissions

The plugin registers a `codegraph` MCP server (`.mcp.json`) exposing 6 tools: `context`, `explore`, `trace`, `impact`, `files`, and `search`. It is marked `alwaysLoad` so the schemas are resident at the moment Claude chooses how to explore — that residency is the whole point, and it costs ~1,000 tokens per session.

**A plugin cannot ship permissions**, so add this yourself to avoid a prompt on every call — in `.claude/settings.json`:

```json
{
  "permissions": {
    "allow": ["mcp__plugin_smart-mcps_codegraph__*"]
  }
}
```

Approve the server when prompted, or pre-approve it in `.claude/settings.local.json` with `"enabledMcpjsonServers": ["codegraph"]`.

If you'd rather not pay the tokens, drop `"alwaysLoad": true` from `.mcp.json`: the tools stay reachable but defer to names-only until loaded, which largely recreates the problem this solves.

### Perplexity

Requires a `PERPLEXITY_API_KEY`. The CLI is spawned by the VS Code extension host, so the key must be in that process's environment — not just a terminal session.

**Recommended:** Install the [mkhl.direnv](https://marketplace.visualstudio.com/items?itemName=mkhl.direnv) VS Code extension and create a per-project `.envrc`:

```bash
cp .envrc.example .envrc   # fill in your key
direnv allow
```

**Shell profile fallback** (all projects share the same key):

```bash
export PERPLEXITY_API_KEY="pplx-..."   # in ~/.bashrc or ~/.profile
```

### NotebookLM

**Step 1 — one-time browser auth:**

```bash
uv tool install notebooklm-mcp-cli
nlm login
```

Re-run `nlm login` when cookies expire. Auth state is stored in `~/.notebooklm-mcp-cli/` and persists across projects.

**Step 2 — seed aliases from your notebook titles:**

```bash
bash scripts/seed-nlm-aliases.sh
```

This registers each notebook as an `nlm` alias (e.g. `ltx-2-3-engineering-...`). Aliases persist globally across projects. Re-run whenever you create new notebooks. Inspect with `nlm alias list`.

**Step 3 — configure the `notebooklm-chat` skill _(required)_:**

Open `skills/notebooklm-chat/SKILL.md` and fill in the notebook map:

```markdown
| Topic | Notebook alias or ID |
| ----- | -------------------- |
| LTX 2.3 / video generation / cinematography | `ltx-2-3-engineering-and-ic-lora-implementation-guide` |
| ControlNet / pose / depth                   | `floed-and-comfyui-controlnet-aux-development-summaries` |
```

Map by **semantic topic** (not just notebook title) so the skill can match natural-language questions. Use the alias slugs from step 2 or raw UUIDs — both work.

Use `/notebooklm-complete` for everything beyond querying: adding sources, creating audio overviews, managing notes.

______________________________________________________________________

## Hooks

The plugin installs a small set of session hooks:

| Hook                                 | What it does                                                                         |
| ------------------------------------ | ------------------------------------------------------------------------------------ |
| `SessionStart` (codegraph)           | Runs `codegraph index --force` detached on session start if `codegraph` is installed |
| `SessionStart` (formatters)          | Best-effort `uv tool install` of ruff and mdformat (with gfm + frontmatter plugins)  |
| `PostToolUse` (Bash)                 | Saves NotebookLM query results as markdown under `docs/research/notebooklm/`         |
| `PostToolUse` (Edit/Write/MultiEdit) | Auto-formats Python (ruff) and Markdown (mdformat) after edits; falls back to `uvx`  |

______________________________________________________________________

## Editing skills

Skills are plain markdown files — edit them directly to customize behaviour for your project. This is intentional: the `notebooklm-chat` notebook map, the perplexity model choices, and any prompt tuning all live in the skill files.

The `codegraph` skill is the exception to "skills are prompts you tune freely": it documents the MCP tools rather than duplicating them, because a skill teaching a second, CLI-shaped path to the same data splits attention and bypasses the staleness banner.

Claude Code loads skills from `.claude/skills/` in the project directory. The repo ships with this symlink already in place:

```text
.claude/skills -> ../skills
```

If you clone fresh and the symlink is missing:

```bash
ln -s ../skills .claude/skills
```

Edits take effect on the next session start — no reinstall needed.

______________________________________________________________________

## CLI tools

Install the Python CLI for local use (needed by the hooks scripts and skills):

```bash
pip install -e .
# or
uv pip install -e .
```

This installs:

| Command                 | Source                    |
| ----------------------- | ------------------------- |
| `smart-mcps-perplexity` | `pplx/cli.py`             |
| `smart-mcps-codegraph`  | `codegraph_mcp/server.py` |

`smart-mcps-codegraph` is the MCP server spawned by `.mcp.json` — you don't run it by hand. Plugin consumers don't need this install step for it either: `.mcp.json` invokes it via `uv run --project ${CLAUDE_PLUGIN_ROOT}`, which resolves dependencies from the plugin's own checkout.

______________________________________________________________________

## Repository layout

```text
.mcp.json              # codegraph MCP server registration (alwaysLoad)

.claude-plugin/
  plugin.json          # plugin metadata (skills path, hooks path)
  marketplace.json     # marketplace listing

skills/
  codegraph/SKILL.md
  notebooklm-chat/SKILL.md      # query only, resolves names via nlm aliases
  notebooklm-complete/SKILL.md  # full management (sources, audio, notes, etc.)
  perplexity/SKILL.md
  plan-to-plan/SKILL.md         # research planning
  apply-research-plan/SKILL.md  # research execution + implementation plan
  plan-notebookllm/SKILL.md     # notebook-grounded planning

scripts/
  seed-nlm-aliases.sh  # populate nlm aliases from notebook titles

hooks/
  hooks.json           # hook definitions
  scripts/             # hook scripts (save_research.py, lint_after_edit.py)

pplx/                  # smart-mcps-perplexity CLI source

codegraph_mcp/
  server.py            # FastMCP proxy — 6 trimmed tools over `codegraph serve --mcp`
```
