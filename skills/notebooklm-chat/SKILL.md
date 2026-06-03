---
name: notebooklm-chat
description: Chat with a NotebookLM notebook by topic. Ask questions and get grounded answers. Configure your notebook map in this file after install.
argument-hint: "[topic or question]"
user-invocable: true
---

# notebooklm-chat

Use the `nlm` CLI to answer questions from your NotebookLM notebooks.

**Auth prerequisite**: `nlm login` must be run once. If any command fails with an auth error, tell the user to run `nlm login`.

## Notebook map

> **After installing this plugin, edit the table below** to match your own notebooks.
> Run `bash scripts/seed-nlm-aliases.sh` once to list your notebooks and their alias slugs,
> then fill in the table. Use alias slugs or raw UUIDs — both work as notebook IDs.

| Topic | Notebook alias or ID |
| ----- | -------------------- |
| example topic / subtopic / keyword | `your-alias-slug-here` |

## Query

Match the user's question to the closest topic in the map, then run:

```bash
nlm notebook query ALIAS_OR_ID "the user's question"
```

To continue a conversation on the same notebook, pass the returned `conversation_id`:

```bash
nlm notebook query ALIAS_OR_ID "follow-up question" --conversation-id CONV_ID
```

## Rules

- Use the map above — no runtime discovery commands.
- If no topic matches, list what's in the map and ask the user to clarify.
- Only query — do not create, delete, or modify anything.
- For anything beyond querying (add source, create audio, manage notes), tell the user to use `/notebooklm-complete`.
