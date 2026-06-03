---
name: notebooklm
description: Query, create, and manage NotebookLM notebooks and sources using the nlm CLI. Use when asked to query a notebook, add sources, create audio/video artifacts, or research across notebooks.
argument-hint: "[notebook name or query]"
user-invocable: true
---

Use the `nlm` CLI via bash for all NotebookLM operations.

**Auth prerequisite**: `nlm login` must be run once in a terminal before using these commands. If any command fails with an auth error, tell the user to run `nlm login` and retry.

## Decision table — pick the right command

| Need | Command |
| ---- | ------- |
| List all notebooks (get IDs) | `nlm notebook list` — always run this first |
| Ask a question about notebook content | `nlm notebook query --notebook ID "question"` |
| Get notebook details | `nlm notebook get ID` or `nlm notebook describe ID` |
| Create a new notebook | `nlm notebook create "Title"` |
| Add a source (URL, text, or file) | `nlm source add --notebook ID --url URL` |
| Describe a source | `nlm source describe --notebook ID --source SOURCE_ID` |
| Create audio overview | `nlm studio create --notebook ID --type audio` |
| Check artifact status | `nlm studio status --notebook ID` |
| Download finished artifact | `nlm download --notebook ID --type audio` |
| Query across multiple notebooks | `nlm cross query "question" --notebooks ID1,ID2` |
| Manage notes | `nlm note --notebook ID --action list\|create\|delete` |

## Step 1 — always list notebooks first

```bash
nlm notebook list
```

This returns IDs. Human names are not accepted directly in most subcommands — you must resolve to an ID first.

## Query a notebook

```bash
nlm notebook query --notebook "NOTEBOOK_ID" "what are the main themes in this source?"
nlm notebook query --notebook "NOTEBOOK_ID" "summarize the key findings"
```

## Add sources

```bash
nlm source add --notebook "NOTEBOOK_ID" --url "https://example.com/paper"
nlm source add --notebook "NOTEBOOK_ID" --text "pasted content here"
```

## Studio artifacts (audio/video)

```bash
# Create — async, returns immediately
nlm studio create --notebook "NOTEBOOK_ID" --type audio

# Poll until done
nlm studio status --notebook "NOTEBOOK_ID"

# Download when status shows complete
nlm download --notebook "NOTEBOOK_ID" --type audio
```

`nlm studio create` is async — always follow with `nlm studio status`. Do not assume the artifact is ready immediately.

## Cross-notebook query

```bash
nlm cross query "compare the approaches in these two papers" --notebooks "ID1,ID2"
```

## Notes

```bash
nlm note --notebook "NOTEBOOK_ID" --action list
nlm note --notebook "NOTEBOOK_ID" --action create --content "key insight"
```

## Usage rules

- **Always run `nlm notebook list` first** to resolve a human notebook name to an ID.
- Do not create new notebooks unless the user explicitly asks.
- `nlm studio create` is async — never report an artifact as ready without checking `nlm studio status`.
- If auth fails at any point, stop and tell the user: `nlm login` must be re-run in a terminal.
