---
name: perplexity
description: Web-grounded search and reasoning using the Perplexity API. Use when you need current information from the web, or need AI-synthesized answers grounded in live sources.
argument-hint: "[question or search query]"
user-invocable: true
---

Use the `smart-mcps-perplexity` CLI via bash for all web-grounded queries.

**Requires**: `PERPLEXITY_API_KEY` environment variable. If unset, stop immediately and tell the user — do not retry.

## Decision table — pick the right subcommand

| Need                                                          | Command  | Model               | Speed          | Cost   |
| ------------------------------------------------------------- | -------- | ------------------- | -------------- | ------ |
| Broad / exploratory / underspecified — discover the landscape | `ask`    | sonar-pro           | Fast (~3 s)    | Low    |
| Specific / constrained — reason precisely over known options  | `reason` | sonar-reasoning-pro | Medium (~10 s) | Medium |

**Pick by how specific the question already is, not by habit:**

- Still vague, no stack/model/criteria named yet → `ask`. Wider retrieval and landscape discovery matter most.
- Already names a concrete model, framework, or pattern and wants a comparison or recommendation → `reason`. Precise reasoning over known constraints matters most.
- Unsure? Ask: is the missing piece *more context* (→ `ask`) or *better reasoning* (→ `reason`)?
- Both handle recency and domain constraints already. The CLI also has `research` and `agent` subcommands, but they are too slow and expensive for agent loops — never invoke them on your own; mention them to the user only if a question genuinely needs multi-hop research, and let the user run them manually.

## ask — factual Q&A, landscape discovery

```bash
smart-mcps-perplexity ask "what are Claude Code hooks"
smart-mcps-perplexity ask "explain this function" --file path/to/script.py
smart-mcps-perplexity ask "summarize recent findings on LoRA" --scientific-research
```

Options:

- `--file PATH` — prepend file contents (code, markdown, any text) to the question; use this instead of copy-pasting large inputs
- `--scientific-research` — restrict search to `arxiv.org`, `huggingface.co`, `github.com`; suited for longer, descriptive technical asks
- `--context-size {low,medium,high}` — see Context size below; default `medium`

**`--scientific-research` is a user-facing mode.** Invoke it when the user asks about papers, models, datasets, or research findings — it is appropriate for full descriptive questions rather than short keyword queries.

## reason — step-by-step analysis over a concrete scenario

```bash
smart-mcps-perplexity reason "should I use MCP tools or CLI skills for code intelligence?"
smart-mcps-perplexity reason "compare these two approaches" --file design.md --context-size high
```

Use for decisions that require weighing multiple factors over a scenario the question already frames. Returns a chain-of-thought response with a conclusion. Takes the same `--file` and `--context-size` flags as `ask`.

## Context size — `--context-size {low,medium,high}`

Controls how much search material the model retrieves; cost and latency scale with it. Applies to both `ask` and `reason` — defaults to `medium`.

- **medium** (default) — most technical questions, implementation discussions, targeted comparisons
- **high** — broad architectural investigations, long or multi-document material, multi-framework comparisons, high-stakes/hard-to-reverse decisions where missing context would hurt the answer
- **low** — narrow, well-defined factual lookups where minimal retrieval suffices

## Output format

The CLI prints **plain markdown text** to stdout — citations are stripped from the output to keep context size low.

## Usage rules

- Do not loop on failures: if the API returns an error, show it to the user and stop.
- Do not use perplexity for questions answerable from the local codebase — prioritize `codegraph context` for exploring code (see the codegraph skill: `skills/codegraph/SKILL.md`) or `Read` instead.
- Pass `--file` instead of copy-pasting large code blocks or documents into the question string.
