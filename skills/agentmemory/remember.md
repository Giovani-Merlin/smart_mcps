---
name: remember
description: Explicitly save an insight, decision, or learning to agentmemory's long-term storage. Use when the user says "remember this", "save this", or wants to preserve knowledge for future sessions.
argument-hint: "[what to remember]"
user-invocable: true
---

The user wants to save this to long-term memory: $ARGUMENTS

Steps:

1. Analyze what the user wants to remember — extract the core insight, decision, or fact.
2. Extract 2-5 searchable concepts (lowercased keyword phrases). Prefer specific terms (`"jwt-refresh-rotation"` beats `"auth"`).
3. Choose a `--type`: `architecture`, `decision`, `workflow`, `bug`, `pattern`, or `code`.
4. Run via bash:

```bash
smart-mcps-agentmemory save "CONTENT" --type TYPE --concepts concept1 concept2
```

5. Confirm to the user that the memory was saved and show the concepts tagged so they know what terms will retrieve it later.

If the command fails with a connection error, the agentmemory backend is not running — tell the user to start it with `~/.agentmemory/start.sh`.
