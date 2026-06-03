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
smart-mcps-agentmemory sessions "${ARGUMENTS:-recent work}" --limit 3
```

Note: if `$ARGUMENTS` is empty, the sessions command uses `"recent work"` as the default search query so the search is not blank.

## Synthesize the handoff

From the results:

1. **Lead with the most relevant recent session** — if `$ARGUMENTS` was provided, pick the session that best matches the topic; otherwise use the most recent session from the `sessions` output.
2. **Summarize**: what was being worked on, the last known state, key decisions made.
3. **Surface recently-modified files**: use `profile.recentActivity` if non-empty. If it is empty (common when observations haven't been indexed yet), use the `cwd` field from the matched sessions to at least name the project directory.
4. **Flag open questions**: if the last session ended on an unanswered question or a TODO, surface that first.
5. **Close with a "next step" pointer** — one concrete action the user can take to continue.

## Error handling

- **No sessions found**: say so clearly and offer to start fresh.
- **Backend down**: tell the user to start it (`~/.agentmemory/start.sh`) — do not invent session details.

Do not invent or infer session content. Only present what the commands actually returned.
