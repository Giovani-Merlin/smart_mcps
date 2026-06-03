---
name: perplexity
description: Web-grounded search and reasoning using the Perplexity API. Use when you need current information from the web, or need AI-synthesized answers grounded in live sources.
argument-hint: "[question or search query]"
user-invocable: true
---

Use the `smart-mcps-perplexity` CLI via bash for all web-grounded queries.

**Requires**: `PERPLEXITY_API_KEY` environment variable. If unset, stop immediately and tell the user — do not retry.

## Decision table — pick the right subcommand

| Need | Command | Speed | Cost |
| ---- | ------- | ----- | ---- |
| Quick factual Q&A | `ask` | Fast (~3 s) | Low |
| Step-by-step analysis of a hard question | `reason` | Medium (~10 s) | Medium |

**Default to `ask`.** It handles 80 % of questions.

## ask — factual Q&A

```bash
smart-mcps-perplexity ask "what are Claude Code hooks"
smart-mcps-perplexity ask "latest FastMCP release" --domains github.com
smart-mcps-perplexity ask "explain this function" --file path/to/script.py
smart-mcps-perplexity ask "summarize recent findings on LoRA" --scientific-research
```

Options:
- `--file PATH` — prepend file contents (code, markdown, any text) to the question; use this instead of copy-pasting large inputs
- `--scientific-research` — restrict search to `arxiv.org`, `huggingface.co`, `github.com`; suited for longer, descriptive technical asks
- `--domains domain1,domain2` — custom domain allowlist (prefix with `-` to exclude); combined with `--scientific-research` if both are set

**`--scientific-research` is a user-facing mode.** Invoke it when the user asks about papers, models, datasets, or research findings — it is appropriate for full descriptive questions rather than short keyword queries.

## reason — step-by-step analysis

```bash
smart-mcps-perplexity reason "should I use MCP tools or CLI skills for code intelligence?"
smart-mcps-perplexity reason "compare these two approaches" --file design.md
```

Use for decisions or analysis that requires weighing multiple factors. Returns a chain-of-thought response with a conclusion.

## Output format

The CLI prints **plain markdown text** to stdout — citations are stripped from the output to keep context size low.

## Usage rules

- Do not loop on failures: if the API returns an error, show it to the user and stop.
- Do not use perplexity for questions answerable from the local codebase — use `codegraph` or `Read` instead.
- Pass `--file` instead of copy-pasting large code blocks or documents into the question string.
