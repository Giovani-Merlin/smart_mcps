---
name: recap
description: Summarize recent agent sessions for the current project. Use when the user asks "recap", "what have we been doing", "this week", "today", or wants a rollup of recent work.
argument-hint: "[today | this week | last N]"
user-invocable: true
---

The user wants a recap. Time window: $ARGUMENTS

Run via bash:

```bash
smart-mcps-agentmemory sessions "recent work" --limit 10
smart-mcps-agentmemory profile
```

Parse the time window from `$ARGUMENTS`:

- `today` → sessions from the current local date
- `this week` → sessions from the last 7 days
- `last N` → most recent N sessions
- empty → default to last 10

Group surviving sessions by calendar date (YYYY-MM-DD). For each date:

- List each session: first prompt (truncated), status, startedAt
- For interesting sessions, run `smart-mcps-agentmemory find "SESSION_TOPIC"` to pull supporting observations

End with a one-line total: "N sessions across M days."

Do not invent sessions. If the window is empty, say so.
