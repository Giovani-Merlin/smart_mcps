---
name: notebooklm
description: Query, create, and manage NotebookLM notebooks and sources using the nlm CLI. Use when asked to query a notebook, add sources, create audio/video artifacts, or research across notebooks.
argument-hint: "[notebook name or query]"
user-invocable: true
---

Use the `nlm` CLI via bash for all NotebookLM operations.

**Auth prerequisite**: `nlm login` must be run once in the terminal before using these commands.

## Core operations

**List notebooks**:

```bash
nlm notebook list
```

**Query a notebook** (chat with its sources):

```bash
nlm notebook query --notebook "NOTEBOOK_NAME_OR_ID" "your question here"
```

**Get notebook details**:

```bash
nlm notebook get "NOTEBOOK_NAME_OR_ID"
nlm notebook describe "NOTEBOOK_NAME_OR_ID"
```

**Create a notebook**:

```bash
nlm notebook create "Notebook Title"
```

## Sources

**Add a source** (url, text, or file):

```bash
nlm source add --notebook "NOTEBOOK_ID" --url "https://..."
nlm source add --notebook "NOTEBOOK_ID" --text "content here"
```

**Describe a source**:

```bash
nlm source describe --notebook "NOTEBOOK_ID" --source "SOURCE_ID"
```

## Studio artifacts

**Create audio overview / other artifacts**:

```bash
nlm studio create --notebook "NOTEBOOK_ID" --type audio
nlm studio status --notebook "NOTEBOOK_ID"
```

**Download an artifact**:

```bash
nlm download --notebook "NOTEBOOK_ID" --type audio
```

## Cross-notebook query

```bash
nlm cross query "your question" --notebooks "ID1,ID2"
```

## Notes

```bash
nlm note --notebook "NOTEBOOK_ID" --action create --content "note text"
nlm note --notebook "NOTEBOOK_ID" --action list
```

## Usage guidelines

- Always run `nlm notebook list` first to find the right notebook ID.
- For long research tasks, prefer `nlm notebook query` over creating new artifacts.
- `nlm studio create` is async — poll with `nlm studio status` until complete.
