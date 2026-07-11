---
name: notebooklm-complete
description: Full NotebookLM management — query, create, add sources, manage notes, and generate audio/video artifacts via the nlm CLI.
argument-hint: "[notebook name, action, or question]"
user-invocable: true
---

Use the `nlm` CLI via bash for all NotebookLM operations.

**Auth prerequisite**: `nlm login` must be run once in a terminal before using these commands. If any command fails with an auth error, tell the user to run `nlm login` and retry.

## Step 1 — always list notebooks first

```bash
nlm notebook list
```

Returns IDs. Notebook names are not accepted directly — resolve to an ID first. Aliases also work as IDs wherever a `NOTEBOOK_ID` is accepted.

## Decision table

| Need                                  | Command                                              |
| ------------------------------------- | ---------------------------------------------------- |
| List all notebooks (get IDs)          | `nlm notebook list`                                  |
| Ask a question about notebook content | `nlm notebook query NOTEBOOK_ID "question"`          |
| Get notebook summary                  | `nlm notebook describe NOTEBOOK_ID`                  |
| Create a new notebook                 | `nlm notebook create "Title"`                        |
| Add a URL source                      | `nlm source add NOTEBOOK_ID --url URL`               |
| Add a text source                     | `nlm source add NOTEBOOK_ID --text "content"`        |
| Add a local file                      | `nlm source add NOTEBOOK_ID --file path/to/file.pdf` |
| Describe a source                     | `nlm source describe SOURCE_ID`                      |
| Create audio overview                 | `nlm audio create NOTEBOOK_ID`                       |
| Check artifact status                 | `nlm studio status NOTEBOOK_ID`                      |
| Download audio artifact               | `nlm download audio NOTEBOOK_ID`                     |
| Query across multiple notebooks       | `nlm cross query "question" --notebooks ID1,ID2`     |
| List notes                            | `nlm note list NOTEBOOK_ID`                          |
| Create a note                         | `nlm note create NOTEBOOK_ID --content "insight"`    |

## Query a notebook

```bash
nlm notebook query NOTEBOOK_ID "what are the main themes?"
nlm notebook query NOTEBOOK_ID "summarize the key findings"
```

## Add sources

```bash
nlm source add NOTEBOOK_ID --url "https://example.com/paper"
nlm source add NOTEBOOK_ID --text "pasted content here"
nlm source add NOTEBOOK_ID --file document.pdf --wait
```

## Audio overview (async)

```bash
# Create — returns immediately, does not block
nlm audio create NOTEBOOK_ID

# Poll until done
nlm studio status NOTEBOOK_ID

# Download when status shows complete
nlm download audio NOTEBOOK_ID
```

Never report an audio artifact as ready without first confirming via `nlm studio status`.

## Notes

```bash
nlm note list NOTEBOOK_ID
nlm note create NOTEBOOK_ID --content "key insight"
```

## Cross-notebook query

```bash
nlm cross query "compare approaches" --notebooks "ID1,ID2"
```

## Usage rules

- Always run `nlm notebook list` first to resolve names to IDs.
- Do not create new notebooks unless the user explicitly asks.
- Never report an artifact as ready without checking `nlm studio status`.
- If auth fails at any point, stop and tell the user: `nlm login` must be re-run in a terminal.
