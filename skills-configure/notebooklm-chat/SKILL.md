---
name: notebooklm-chat
description: Chat with a NotebookLM notebook by topic. Ask questions and get grounded answers. Configure your notebook map in this file after install.
argument-hint: "[topic or question]"
user-invocable: true
---

<!-- CONFIGURE ME
     This is the locked-alias version of the notebooklm-chat skill.
     It uses a hardcoded notebook map so you don't pay the cost of `nlm notebook list`
     on every query.

     To use it:
       1. Run `nlm notebook list` to see your notebooks and their IDs or aliases.
       2. Fill in the table below with the topics and aliases/IDs you want to expose.
       3. Copy this file over skills/notebooklm-chat/SKILL.md in your project.

     The default skills/notebooklm-chat/SKILL.md uses dynamic discovery (`nlm notebook list`)
     instead — use that if you prefer no configuration overhead.
-->

# notebooklm-chat

Use the `nlm` CLI to answer questions from your NotebookLM notebooks.

**Auth prerequisite**: `nlm login` must be run once. If any command fails
with an auth error, tell the user to run `nlm login`.

## Notebook map

<!-- Add one row per notebook you want accessible. Use the alias (if set) or the UUID. -->

| Topic       | Notebook alias or ID |
| ----------- | -------------------- |
| AgentMemory | `agentmemory`        |

## Query

Match the user's question to the closest topic in the map, then run:

```bash
nlm notebook query --json ALIAS_OR_ID "the user's question" | jq -r '.value.answer' | sed 's/ *\[[0-9][0-9, -]*\]//g'
```

To continue a conversation on the same notebook, pass the returned `conversation_id`:

```bash
# First capture the full JSON to extract both answer and conversation_id
RESULT=$(nlm notebook query --json ALIAS_OR_ID "follow-up question" --conversation-id CONV_ID)
echo "$RESULT" | jq -r '.value.answer' | sed 's/ *\[[0-9][0-9, -]*\]//g'
CONV_ID=$(echo "$RESULT" | jq -r '.value.conversation_id')
```

## Rules

- Use the map above — no runtime discovery commands.
- If no topic matches, list what's in the map and ask the user to clarify.
- Only query — do not create, delete, or modify anything.
- For anything beyond querying (add source, create audio, manage notes),
  tell the user to use `/notebooklm-complete`.
