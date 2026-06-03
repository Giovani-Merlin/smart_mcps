---
name: handoff
description: Resume the most recent agent session for the current project. Use when the user says "where were we", "resume", "handoff", or "pick up where I left off".
argument-hint: "[optional topic]"
user-invocable: true
---

The user wants to resume work. Context hint: $ARGUMENTS

Run these two commands:

```bash
smart-mcps-agentmemory profile
smart-mcps-agentmemory sessions "$ARGUMENTS" --limit 3
```

From the results:

1. If `$ARGUMENTS` is provided, lead with the most relevant recent session that matches the topic.
2. Otherwise, use the most recent session from `sessions` output.
3. Summarize: what was being worked on, key files touched, last known state.
4. If the session ended on an unanswered question, surface that first.
5. End with a short "next step?" pointer the user can act on.

Do not invent session details. If no sessions are found, say so and offer to start fresh.
