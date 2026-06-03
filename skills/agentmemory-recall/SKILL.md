---
name: recall
description: Search agentmemory for past observations, sessions, and learnings about a topic. Use when the user says "recall", "remember", "what did we do", or needs context from past sessions.
argument-hint: "[search query]"
user-invocable: true
---

The user wants to recall past context about: $ARGUMENTS

```bash
smart-mcps-agentmemory find "$ARGUMENTS" --limit 10
```

If the command returns zero observations, retry with `--depth deep` (makes two extra API calls — slower but pulls from all historical data):

```bash
smart-mcps-agentmemory find "$ARGUMENTS" --limit 10 --depth deep
```

## Interpreting results

The output is JSON with three arrays: `observations`, `memories`, `lessons`.

- **observations**: raw captured tool events and session notes, scored 0.0–1.0
  - Score ≥ 0.7 → high confidence, lead with these
  - Score 0.5–0.7 → medium confidence, mention as supporting context
  - Score < 0.5 → low confidence, mention briefly or omit if irrelevant
- **memories**: curated durable facts (manually saved)
- **lessons**: confidence-scored learnings extracted across sessions

Present results in readable prose, not raw JSON. Highlight what is most relevant to `$ARGUMENTS`.

## Error handling

- **Connection refused / ECONNREFUSED**: the agentmemory backend is not running. Tell the user to start it: `~/.agentmemory/start.sh` (or `docker compose up -d` if using Docker).
- **Empty results after `--depth deep`**: no prior context exists for this project/topic yet. Say so clearly — do not hallucinate observations.

**Never invent or infer observations.** Only present what the command actually returned.
