---
name: recap
description: Summarize what happened in recent sessions for the current project. Use when the user says "recap", "what happened", "catch me up", or wants a summary of recent activity.
argument-hint: "[optional topic or timeframe]"
user-invocable: true
---

The user wants a recap of recent work. Topic hint: $ARGUMENTS

```bash
smart-mcps-agentmemory sessions "${ARGUMENTS:-recent work}" --limit 10
smart-mcps-agentmemory profile
```

## Summarize the results

From the sessions output, produce a concise recap:

- Group sessions by theme or date if multiple are returned
- For each session: what was the goal, what was done, what was the outcome
- Highlight any unresolved issues or open threads
- End with a one-line total: "N sessions across M days"

Parse any time window from `$ARGUMENTS`:
- `today` → sessions from the current date
- `this week` → last 7 days
- `last N` → most recent N sessions
- empty → default to last 10

## Backend down fallback

If the command fails with a connection error (backend not running), fall back to summarizing what is visible in the current conversation context instead. Make clear that the agentmemory backend is not running and the recap is based only on the current session.

Tell the user to start the backend: `~/.agentmemory/start.sh` (or `docker compose up -d`).

Do not invent session details. Only present what the command or the current conversation actually contains.
