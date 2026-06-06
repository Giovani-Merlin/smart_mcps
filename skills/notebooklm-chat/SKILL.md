---
name: notebooklm-chat
description: Chat with a NotebookLM notebook by topic. Ask questions and get grounded answers. Configure your notebook map in this file after install.
argument-hint: "[topic or question]"
user-invocable: true
---

# notebooklm-chat

Use the `nlm` CLI to answer questions from your NotebookLM notebooks.

**Auth prerequisite**: `nlm login` must be run once. If any command fails
with an auth error, or if `nlm notebook list` fails, tell the user to run `nlm login`.

## Step 1 — discover available notebooks

```bash
nlm notebook list
```

Output is a JSON array: `[{"id": "...", "title": "...", "source_count": N, "updated_at": "..."}]`

- If the command fails → tell the user to run `nlm login` and stop.
- If only one notebook is returned → use it without asking.
- If multiple notebooks are returned → pick the one whose `title` best matches the topic or question in `$ARGUMENTS`. If two or more are equally plausible, list their titles and ask the user to choose.

## Step 2 — query the chosen notebook

```bash
nlm notebook query --json NOTEBOOK_ID "the user's question" | jq -r '.value.answer' | sed 's/ *\[[0-9][0-9, -]*\]//g'
```

Use the `id` field from the list output, not the title.

To continue a conversation on the same notebook, capture and reuse `conversation_id`:

```bash
RESULT=$(nlm notebook query --json NOTEBOOK_ID "follow-up question" --conversation-id CONV_ID)
echo "$RESULT" | jq -r '.value.answer' | sed 's/ *\[[0-9][0-9, -]*\]//g'
CONV_ID=$(echo "$RESULT" | jq -r '.value.conversation_id')
```

## Rules

- Always run `nlm notebook list` first — do not hardcode IDs or aliases.
- Only query — do not create, delete, or modify anything.
- For anything beyond querying (add source, create audio, manage notes),
  tell the user to use `/notebooklm-complete`.
