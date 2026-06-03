---
name: perplexity
description: Web-grounded search and reasoning using the Perplexity API. Use when you need current information from the web, want to find URLs, or need AI-synthesized answers with citations.
argument-hint: "[question or search query]"
user-invocable: true
---

Use the `smart-mcps-perplexity` CLI via bash for all web-grounded queries.

**Requires**: `PERPLEXITY_API_KEY` environment variable set.

## Choose the right subcommand

| Need | Command |
| --- | --- |
| Quick factual Q&A with citations | `ask` |
| Find specific URLs or recent news | `search` |
| Deep multi-source investigation (30s+) | `research` |
| Step-by-step analysis of a complex question | `reason` |

## ask — quick Q&A

```bash
smart-mcps-perplexity ask "your question"
smart-mcps-perplexity ask "latest Claude Code features" --recency week
smart-mcps-perplexity ask "FastMCP docs" --domains docs.anthropic.com,github.com
```

Options: `--recency hour|day|week|month|year`, `--domains domain1,domain2` (prefix with `-` to exclude)

## search — find URLs and facts

```bash
smart-mcps-perplexity search "Claude Code plugin system"
smart-mcps-perplexity search "agentmemory rohitg00" --domains github.com
```

## research — in-depth investigation

```bash
smart-mcps-perplexity research "best practices for MCP server design"
```

Use for complex, multi-source questions. Slow (30s+), but thorough.

## reason — step-by-step analysis

```bash
smart-mcps-perplexity reason "should I use MCP tools or CLI skills for code intelligence?"
```

Use for decisions or analysis that require weighing multiple factors.

## Usage guidelines

- Default to `ask` for most questions — it is fast and includes citations.
- Use `search` when you need the actual URLs, not just an answer.
- Use `research` sparingly — it is expensive and slow.
- Always show citations from the output to the user so they can verify.
