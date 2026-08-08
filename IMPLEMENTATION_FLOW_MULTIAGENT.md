<img src="https://r2cdn.perplexity.ai/pplx-full-logo-primary-dark%402x.png" style="height:64px;margin-right:32px"/>

# I want to get deeper on the orchestrator part with the "already broken in groups tasks". I've a custom made claude sessions analyzer and when we have sub-agents we know easily from jsonl that it is a sub-agent returning information to the orchestrator and well, they can communicate. How can I do a custom made orchestrator with a python script? Lets say I've broken the tasks in groups and I launch sessions (if possible, attached to a specific session to mark it) that will execute the group of tasks with an already created shared prefix (well, first prompt, already with the document context we discussed), then the script is sending the task to the groups using claude cli (or the orchestrator is using the script...) - how can I ensure the communication with the orchestrator and the sub-agents and be sure they're linked? If I let one orchestrator launching sub-agents it will work as we know but I want to be with a helper script for doing so instead of wasting a lot of tokens.

Lets say a sub-agent finish a group, then a reviewer verify something is wrong and the context of the sub-agent is smaller than a threshold, superpowers does launch a new fresh sub-agent which is extremelly wastefull, I want to send again to the sub-agent a communication "this was not good, you must ... ... .." then it continues and then the reviewer keeps the context and see the rest. Then if approved or it's "too hard" it send back to the orchestrator just to adapt the tasks list instead of launching more agents. Then it can note or go to the next group tasks and of course spawning a new context (but with the shared prefix). It looks like it matches these papers but I'm not sure how to do it specifically with claude

You can get what you want by treating Claude Code sessions themselves as your “subagents” and using a Python orchestrator that:

* creates one background session per group (each in its own worktree),
* keeps a mapping `group_id → session_id`,
* talks to those sessions via `claude -r` (resume) instead of spinning up new ones, and
* reads/writes structured JSON messages so the orchestration is machine‑driven, not prompt‑driven.

Claude’s subagent mechanism still lives inside each session, but your Python orchestrator never has to spawn fresh top‑level sessions just because a reviewer found issues; it can re‑enter the same session and tell Claude “continue this group, fix X, then re‑run verification” with full context.[^1][^2]

Below is how to wire this concretely.

## Core pattern: sessions as workers, Python as orchestrator

Claude Code already gives you exactly what you need for external orchestration:

* **Background sessions**: `claude --bg "task"` starts a worker session in the background and prints its ID; you can attach, stop, respawn, or read logs later.
* **Resume by ID/name**: `claude -r "<session-id-or-name>" "query"` lets you send further prompts to the same session, preserving its context.
* **Worktrees**: `isolation: worktree` in agent frontmatter or tools like `claude-wt` create an isolated git worktree per session, so workers don’t clash on files.[^3][^4][^2]
* **JSONL transcripts**: sessions are stored under `~/.claude/projects` as JSONL, with each line a structured event; tooling like cc‑se and claude‑devtools shows how to parse them programmatically.[^5][^6][^7]

Your Python orchestrator doesn’t need to “talk directly to subagents”; it talks to sessions via the CLI, and those sessions use Claude’s internal subagents (coder, reviewer, etc.) as needed. All linking is done by session IDs and your own `group_id` metadata.[^5]

## Linking groups to sessions

A minimal schema on the orchestrator side:

```python
@dataclass
class GroupSession:
    group_id: str
    service: str
    session_id: str   # Claude Code session id or name
    worktree_path: str
    status: Literal["ready","running","blocked","completed","failed"]
```

When you’ve “already broken tasks into groups”, you:

1. **Create a worktree per group** (or per service):

```bash
git worktree add ../proj-group-G1 -b group/G1
```

jlowin’s `claude-wt` does this for you and returns an ID, which is a good reference implementation.[^4]
2. **Launch a background session per group from Python**:

```python
import subprocess, json

def start_group_session(group_id, worktree_path, agent_name=None):
    cmd = [
        "claude",
        "--bg",
        "--cwd", worktree_path,
        # optional: pick a specific agent definition
    ]
    if agent_name:
        cmd += ["--agent", agent_name]

    # First arg after flags is the initial prompt
    cmd += [f"START GROUP {group_id} (see JSON spec)"]

    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    # Claude prints something like: "Started background session 7c5dcf5d ..."
    session_id = extract_session_id(proc.stdout)
    return session_id
```

The CLI docs show `--bg` printing the background session ID and commands to manage it, which you parse in `extract_session_id`.
3. **Store `group_id → session_id`** in your orchestrator state. That mapping is what “links” the orchestrator to each worker: all subsequent communication goes to that `session_id` via `claude -r` or `claude logs`.[^5]

You don’t need a special Claude feature to “mark” subagents; your Python orchestration layer is the source of truth.

## Communication protocol: structured JSON between orchestrator and sessions

To avoid prompt‑fragility and get tight control, make the workers speak JSON. For each group session:

* The orchestrator sends a prompt of the form:

> You are the coder subagent for group G1.
> The group spec is below, in JSON.
> Consume it, then respond **only** with a JSON object matching this schema:
> `{ "groupId": "G1", "status": "...", "summary": "...", "verification": {...}, "surprises": [...] }`.
* The worker responds with machine‑parseable JSON (your `subagent final report` schema from the previous answer).

In Python, non‑interactive calls look like:

```python
def send_to_session(session_id, prompt, model="sonnet"):
    cmd = ["claude", "-r", session_id, "-p", prompt, "--model", model]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return proc.stdout  # contains Claude's reply, including JSON
```

This is exactly the pattern used in “jsonl‑to‑pdf” and automation pipelines that call `claude -p` inside scripts: they give Claude a summary and a strict output contract, parse the JSON from stdout, and drive further logic from that.[^8][^9]

For background sessions, you can either:

* Attach temporarily (`claude attach <id>`) if you want interactive debugging, or
* Use `claude logs <id>` from Python to fetch the latest assistant message and parse it.[^7]

Because you always pass `session_id`, you know you’re talking to the same worker context; the orchestrator doesn’t care which internal subagent Claude used to produce the response.[^2][^1]

## Keeping workers “warm” instead of spawning fresh subagents

Your specific pain point:

> “Superpowers does launch a new fresh sub-agent which is extremely wasteful, I want to send again to the sub-agent a communication ‘this was not good, you must …’ then it continues and then the reviewer keeps the context and see the rest.”

You solve this by **not launching a new session** when something is wrong, and by **encoding the iteration state in your JSON + prompts**:

1. **Round 1**: coder session for group G1 runs; returns JSON:

```json
{
  "groupId": "G1",
  "status": "warning",
  "summary": "Implemented X but tests Y failed.",
  "verification": { ... },
  "surprises": [ { "kind": "test_failure", ... } ]
}
```

2. **Reviewer** (either inside the same session via an internal reviewer subagent, or as a *second* session tied to the same group) inspects the diff and writes its own JSON verdict:

```json
{
  "groupId": "G1",
  "reviewStatus": "changes_required",
  "notes": [ "Function foo() breaks bar() invariants" ]
}
```

3. **Orchestrator decides to continue with the same coder session** because:
    * context size for that session (which you can estimate from JSONL or your analyzer) is below a threshold, and
    * the issues are local, not structural.

It sends a new prompt to the **same `session_id`**:

> Group G1 needs changes based on this review JSON: `<review-json>`.
> You are the same coder session working on G1; **do not start a new subagent**.
> Apply the requested fixes, re‑run tests, and respond again with the same JSON schema.
4. The same worker session continues; no new Claude Code session is created. Inside that session, Claude may or may not spawn nested subagents (reviewer, tester) via the `Agent` tool, but the *outer* context and your mapping remain stable.[^1][^2]

The key is: your orchestrator’s “unit of identity” is the session ID, not the internal subagent; workers remain warm until you explicitly stop or consider the group complete.

If you really want a separate reviewer but still share context, you can run **two sessions per group**:

* `G1-coder`: main implementation worker
* `G1-reviewer`: review worker that uses Read/Grep/Git tools and **pulls diffs from the same worktree**

Both are linked to the same `group_id` and `worktree_path`. When reviewer finds issues, it writes JSON back; orchestrator then sends that JSON to `G1-coder` via `claude -r`, as above. Reviewer session stays warm too; when coder posts updated diff, reviewer resumes and checks again.[^3][^4]

## Ensuring sessions are “attached” to the orchestrator

There are three practical ways to ensure workers are linked to your orchestrator:

1. **Naming \& metadata**
Use consistent naming and tags in prompts and branch/worktree names, e.g.:
    * Worktree: `proj-group-G1`
    * Branch: `group/G1`
    * Session name (if you use `claude -r "name"`): `group-G1-coder`

You can embed `group_id` in the first turn’s prompt and in the returned JSON to cross‑check that the session really belongs to that group.
2. **Session JSONL analysis**
You already have a custom sessions analyzer; libraries like `claude-code-sessions-explorer` and `claude-code-replay` show the JSONL schema and how to filter by subagent, tools, and content.[^10][^5]

You can:
    * scan `~/.claude/projects` for lines where `metadata.group_id == "G1"`,
    * ensure there is exactly one active session per group (or two, if coder+reviewer), and
    * estimate context size/tokens per session to decide whether to reuse or retire it.[^6][^7]
3. **CLI `--agents` and `--append-subagent-system-prompt`**
When you launch sessions from Python, you can define their internal subagents via `--agents` (JSON) and append a small identifier to every subagent system prompt via `--append-subagent-system-prompt`, e.g. “You are working for orchestrator O1, group G1.”[^2]

That doesn’t give you direct programmatic control of subagent reuse, but it gives clarity and helps your log analyzer correlate internal subagent trees with each top‑level group.

In all three cases, the authoritative link is your mapping table (`group_id → session_id, worktree_path`), persisted in a small SQLite/JSON file and reloaded on orchestrator startup.

## Python orchestrator loop sketch

Putting it together, the main loop looks like:

```python
groups = load_groups_json()  # from Plan Grouper
sessions = {}

# 1. Launch sessions for ready groups
for g in groups_ready_to_run(groups):
    wt_path = create_worktree_for_group(g)  # or use claude-wt
    sess_id = start_group_session(g["id"], wt_path, agent_name="group-coder")
    sessions[g["id"]] = GroupSession(
        group_id=g["id"],
        service=g["service"],
        session_id=sess_id,
        worktree_path=wt_path,
        status="running",
    )

# 2. Poll workers and reviewers
while not all_complete(groups):
    for g in groups:
        sess = sessions[g["id"]]

        if sess.status == "running":
            report = fetch_latest_report(sess.session_id)  # from stdout or logs
            decision = analyze_report(report)  # success, changes_required, too_hard

            if decision.kind == "success":
                sess.status = "completed"
                unblock_dependents(groups, g["id"])

            elif decision.kind == "changes_required" and report_context_size(sess.session_id) < CONTEXT_THRESHOLD:
                # Reuse same worker session
                prompt = build_fix_prompt(g, report, decision)
                send_to_session(sess.session_id, prompt)

            elif decision.kind == "too_hard":
                sess.status = "failed"
                rewrite_groups_and_dependencies(groups, g, report)
                # Possibly spawn a new group or escalate

    sleep(POLL_INTERVAL)
```

* `fetch_latest_report` can use `claude -r sess_id -p "Emit current status as JSON."` or `claude logs sess_id`.
* `report_context_size` uses your JSONL analyzer to compute approximate tokens.[^7][^5]
* `rewrite_groups_and_dependencies` implements your “reality changed” behavior: adjust group specs and the downstream dependency graph before launching new sessions.

Notice that **Claude never decides when to spawn a new top‑level session** here; your script does. Claude’s internal subagents remain bounded inside each worker session, and your orchestration graph stays outside the LLM context as recommended by dynamic workflows and multi‑agent orchestration work.[^11][^12][^13][^14]

## Why this matches the papers \& Claude patterns

* Papers like Co‑Coder and runtime‑structured decompositions explicitly treat orchestration as an external scheduler working on a dependency graph, with each worker having its own local context and difficulty score; they don’t push orchestration complexity into the models.[^13][^14]
* Dynamic workflows in Claude Code show the same “plan in code, execute via many agents” pattern, where the plan lives in a script, not in a huge prompt.[^12][^11]
* CLI‑driven automation pipelines (e.g., jsonl‑to‑build‑log) demonstrate how to call `claude -p` and `claude -r` from scripts, parse JSON output, and avoid wasting tokens on repeated setup or huge summaries.[^9][^8]

Your Python orchestrator is essentially a small, custom dynamic workflow runner that:

* uses your own grouping granularity and difficulty scores,
* keeps workers warm and reuses their context via `-r` instead of new sessions,
* lets reviewers and coders coordinate via JSON and shared worktrees,
* and only rewrites groups/downstream dependencies when the reports say “too hard” or “surprise”.

All of that is supported by Claude Code’s CLI, subagent, worktree, and logging architecture; you don’t need the API, and you avoid the “spawn a fresh subagent every time something goes wrong” pattern that wastes tokens and discards useful context.[^4][^7][^3][^2][^5]
<span style="display:none">[^15][^16][^17][^18][^19][^20]</span>

<div align="center">⁂</div>

[^1]: https://code.claude.com/docs/de/sub-agents

[^2]: https://www.perplexity.ai/search/9db78848-64ee-4a66-8c4a-6ba1976ea402

[^3]: https://code.claude.com/docs/en/worktrees

[^4]: https://github.com/jlowin/claude-wt

[^5]: https://libraries.io/go/github.com%2Fweirdgiraffe%2Fclaude-code-sessions-explorer

[^6]: https://claude-world.com/tutorials/s16-session-storage/

[^7]: https://claude-dev.tools/docs

[^8]: https://code.claude.com/docs/en/cli-reference

[^9]: https://dev.to/ji_ai/turning-105-session-logs-into-build-logs-a-claude-code-automation-pipeline-30lg

[^10]: https://github.com/glebmish/claude-code-replay/blob/main/docs/log-format.md

[^11]: https://claude.com/blog/introducing-dynamic-workflows-in-claude-code

[^12]: https://claude.com/blog/a-harness-for-every-task-dynamic-workflows-in-claude-code

[^13]: https://papers.cool/arxiv/2606.00953

[^14]: https://arxiv.org/html/2605.15425

[^15]: https://eran.sandler.co.il/post/2025-09-22-cc-sessions-cli/

[^16]: https://code.claude.com/docs/en/sub-agents

[^17]: https://blakecrosley.com/guides/claude-code

[^18]: https://startdebugging.net/2026/04/export-claude-code-conversations-to-pdf-with-jsonl-to-pdf/

[^19]: https://code.visualstudio.com/updates/v1_113

[^20]: https://mcpmarket.com/tools/skills/session-log-analytics

