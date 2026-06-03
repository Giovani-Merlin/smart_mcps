---
name: perplexity
description: Web-grounded search and reasoning using the Perplexity API. Use when you need current information from the web, want to find URLs, or need AI-synthesized answers with citations.
argument-hint: "[question or search query]"
user-invocable: true
---

Use the `smart-mcps-perplexity` CLI via bash for all web-grounded queries.

**Requires**: `PERPLEXITY_API_KEY` environment variable. If unset, stop immediately and tell the user — do not retry.

## Decision table — pick the right subcommand

| Need | Command | Speed | Cost |
| ---- | ------- | ----- | ---- |
| Quick factual Q&A with citations | `ask` | Fast (~3 s) | Low |
| Find specific URLs or recent news | `search` | Fast (~3 s) | Low |
| Deep multi-source investigation | `research` | Slow (30–60 s) | High (5×) |
| Step-by-step analysis of a hard question | `reason` | Medium (~10 s) | Medium |

**Default to `ask`.** It handles 80 % of questions and includes citations.

## ask — factual Q&A

```bash
smart-mcps-perplexity ask "what are Claude Code hooks"
smart-mcps-perplexity ask "latest FastMCP release" --recency week
smart-mcps-perplexity ask "agentmemory rohitg00" --domains github.com
```

Options:
- `--recency hour|day|week|month|year` — filter to recent results
- `--domains domain1,domain2` — restrict sources (prefix with `-` to exclude)

## search — find URLs

```bash
smart-mcps-perplexity search "Claude Code plugin system documentation"
smart-mcps-perplexity search "agentmemory github" --domains github.com
```

Use when the user explicitly needs URLs, not just an answer. Returns links alongside the answer.

## research — deep investigation

```bash
smart-mcps-perplexity research "best practices for MCP server design in 2025"
```

Use for complex, multi-source questions. Takes 30–60 s and costs ~5× more than `ask`. Use sparingly.

## reason — step-by-step analysis

```bash
smart-mcps-perplexity reason "should I use MCP tools or CLI skills for code intelligence?"
```

Use for decisions or analysis that requires weighing multiple factors. Returns a chain-of-thought response with a conclusion.

## Output format

The CLI prints **plain markdown text** to stdout — the answer followed by numbered source citations at the end, like:

```
Claude Code hooks are shell commands that run at specific lifecycle events...

Sources:
1. https://docs.anthropic.com/...
2. https://github.com/...
```

**Always show the citations to the user** — do not paraphrase without sources.

## Usage rules

- Do not use `research` unless the user explicitly asks for a deep investigation — it is slow and expensive.
- Do not loop on failures: if the API returns an error, show it to the user and stop.
- Do not use perplexity for questions answerable from the local codebase — use `codegraph` or `Read` instead.
