# smart-mcps

A Claude Code plugin that installs **skills** and **session hooks** for codegraph, NotebookLM, and Perplexity. All tools run as CLI commands — no MCP servers, no context-window pollution.

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

---

## Skills

| Skill | Trigger | What it does |
| ----- | ------- | ------------ |
| `codegraph` | `/codegraph` | Symbol lookup, call graph tracing, impact analysis via `codegraph` CLI |
| `notebooklm-chat` | `/notebooklm-chat` | Chat with a notebook by topic — query only, **requires configuring your notebook map** in the skill file |
| `notebooklm-complete` | `/notebooklm-complete` | Full NotebookLM management: query, create, add sources, audio/video artifacts |
| `perplexity` | `/perplexity` | Web-grounded search, research, and reasoning via `smart-mcps-perplexity` CLI |
| `plan-to-plan` | `/plan-to-plan` | Research planning — decomposes a topic into tagged external-knowledge questions and writes an approved `research_plan.md` |
| `apply-research-plan` | `/apply-research-plan` | Executes an approved `research_plan.md` via subagents, then writes `research_answers.md` and a concrete `implementation_plan.md` |
| `plan-notebookllm` | `/plan-notebookllm` | Notebook-grounded planning — queries a NotebookLM notebook before producing a plan |

---

## Setup (per tool)

### Codegraph

Requires an index built from your project's source. Run once per project:

```bash
pip install codegraph   # or: uv tool install codegraph
codegraph init
codegraph index
```

The Setup hook runs `codegraph index` automatically on each session start if `codegraph` is installed and an index exists.

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

---

## Hooks

The plugin installs a small set of session hooks:

| Hook | What it does |
| ---- | ------------ |
| `Setup` | Runs `codegraph index` on session start if `codegraph` is installed and an index exists |
| `postToolUse` (Bash) | Saves NotebookLM query results as markdown under `docs/research/notebooklm/` |
| `postToolUse` (edit/write) | Auto-fixes Python (ruff) and Markdown (markdownlint-cli2) after edits |

---

## Editing skills

Skills are plain markdown files — edit them directly to customize behaviour for your project. This is intentional: the `notebooklm-chat` notebook map, the perplexity model choices, and any prompt tuning all live in the skill files.

Claude Code loads skills from `.claude/skills/` in the project directory. The repo ships with this symlink already in place:

```text
.claude/skills -> ../skills
```

If you clone fresh and the symlink is missing:

```bash
ln -s ../skills .claude/skills
```

Edits take effect on the next session start — no reinstall needed.

---

## CLI tools

Install the Python CLI for local use (needed by the hooks scripts and skills):

```bash
pip install -e .
# or
uv pip install -e .
```

This installs:

| Command | Source |
| ------- | ------ |
| `smart-mcps-perplexity` | `pplx/cli.py` |

---

## Repository layout

```text
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
```
