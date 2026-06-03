---
name: recall
description: Search agentmemory for past observations, sessions, and learnings about a topic. Use when the user says "recall", "remember", "what did we do", or needs context from past sessions.
argument-hint: "[search query]"
user-invocable: true
---

The user wants to recall past context about: $ARGUMENTS

Run via bash:

```bash
smart-mcps-agentmemory find "$ARGUMENTS" --limit 10
```

Present the returned JSON to the user in a readable format:

- For each observation show its type, title, and key facts
- Highlight observations with high importance scores
- Show any linked memories or lessons if present

If you need deeper results (including session crystals and insights), add `--depth deep`.

**Do NOT make up or hallucinate observations.** Only present what the command actually returned. If the command fails with a connection error, the agentmemory backend is not running — tell the user to start it with `~/.agentmemory/start.sh`.
