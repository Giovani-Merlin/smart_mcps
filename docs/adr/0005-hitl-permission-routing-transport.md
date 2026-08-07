# ADR 0005 — Transport for live PreToolUse permission routing (proposal only)

> **Status: proposed, not implemented.** This document is a design proposal for
> a future implementation plan. It changes no code under `orchestrator/` or
> `hooks/` — it exists to settle the transport question before that plan is
> written.

## Context

Every escalation the orchestrator knows how to raise today is *report-then-
resume*: a coder finishes its turn, the review loop (running as an async
coroutine) notices something — a question, a stuck state, a merge conflict —
and calls `EscalationBroker.raise_escalation()`
(`orchestrator/execution/escalation.py:95-124`) from a worker thread via
`asyncio.to_thread`. That call writes `request-<id>.json` into
`paths.escalations_dir`, blocks polling for `response-<id>.json`, and returns
once the operator (or a timeout) answers. The nine `EscalationKind` values
(`orchestrator/model.py:165-182`) are all this shape — six decision points
plus three `interactive`-tier approval gates. None of them can *block a tool
call before it runs*, because the broker is only ever reached from the
orchestrator's own process, after a turn has already ended.

The gap: a coder's Claude Code process runs `git push --force`, deletes a
migration, or otherwise trips a genuinely dangerous action mid-turn. Today's
only defense is prompt-level — `coder.md` tells the coder to retry a denied
command up to three times, then give up and report
`CoderReport.status == "permission_denied"` with `denied_command` populated
(`model.py:133`). That is a *post-hoc* signal: the coder already stopped
itself (or was denied by a static allowlist) before the orchestrator ever
hears about it. A `PreToolUse` hook is different in kind — it runs
synchronously inside the coder's own `claude -p` subprocess tree, can inspect
the exact tool call, and can return a decision (`allow`/`deny`/`ask`) that
takes effect *before* the tool executes. That is the capability today's
architecture is missing.

The problem this ADR resolves: **a `PreToolUse` hook process is short-lived —
it starts, decides, and exits within one tool call. It has no existing
channel back to the long-running orchestrator process that owns the group's
`EscalationBroker` and `asyncio` event loop.** Building the hook itself is
straightforward; the hard part is wiring it into the same human channel the
rest of the run already uses, rather than inventing a second, disconnected
approval path.

## Candidate transports

### A — Hook polls the existing `escalations_dir` file convention

The hook process writes its own `request-<id>.json` into the same
`paths.escalations_dir` the broker already uses (same directory, same atomic
write pattern, same file naming scheme it already knows), then polls in a
tight loop for a `response-<id>.json` — exactly the shape
`EscalationBroker.raise_escalation()` already implements, just re-implemented
as the *writer* instead of the *poller* on the request side, and inverted:
the *broker* now watches for new request files instead of only ever creating
them itself. Concretely: `EscalationBroker` gains a lightweight file-watcher
(a `pending_escalations`-style directory scan, already present at
`escalation.py`'s bottom) that notices a new `PERMISSION_REQUEST` kind of
request file appears, surfaces it as a normal escalation into the operator
channel, and writes the response file once answered. The hook process itself
just blocks on `response_path.is_file()` with the same poll interval the
broker already uses, and exits with whatever exit code / stdout JSON
`PreToolUse` hooks use to signal allow/deny.

**Pros:** zero new infrastructure — reuses the same directory, the same
atomic-write helper (`atomic_write_text`), the same request/response Pydantic
models (`EscalationRequest`/`EscalationResponse`), the same CLI surface an
operator already watches (`status`/answering commands operate on this
directory today). The hook is a dumb file poller, which matches this repo's
stated preference for "deliberately boring" transports for human-latency
waits (see `escalation.py`'s own module docstring). No new process, port, or
socket lifecycle to manage per run.

**Cons:** the *broker* side needs an active watcher for new request files
instead of only ever writing them — today it's write-then-poll-response, one
direction; this flips one code path to poll-for-new-requests, which needs to
run continuously (not just when a group happens to call
`raise_escalation`), most naturally as a small background task added to the
run's asyncio event loop rather than living only inside a per-group
coroutine. Filesystem polling latency (bounded by `poll_interval_s`) adds to
every gated tool call, on both the hook's wait and the broker's discovery —
acceptable for the multi-second human-latency waits this system already
tolerates, but worth naming since this path is now *inside* a tool call
rather than between turns.

### B — Per-run local socket or named pipe

The orchestrator opens a Unix domain socket (or named pipe) at run start,
address recorded somewhere the hook can find it (e.g. an env var set by the
hook registration, or a well-known path under the run's own directory). The
hook connects, sends a structured request, and blocks on the socket read for
a response. The broker side runs a small asyncio server (`asyncio.start_unix_server`) that already lives naturally on the same event loop as
the rest of the run, so no new watcher/polling primitive is needed — one
listener, one connection per permission request, dispatched straight into
the existing escalation machinery.

**Pros:** lower latency (no poll interval — the socket read unblocks the
instant the broker writes), and the listener maps directly onto the existing
event loop with no filesystem race window.

**Cons:** a new moving part this repo doesn't have today — socket lifecycle
(create at run start, clean up on run end/crash, handle a stale socket file
from a crashed prior run), and it breaks the "boring, resumable, cheap to
inspect" trait that the file-based convention gives the rest of the
escalation channel for free: `escalations_dir` contents survive a crash and
are directly readable/greppable by an operator or a resumed run;
socket-in-flight state does not. Cross-platform pipe semantics also differ
(Unix domain sockets vs. Windows named pipes) if this project ever needs to
run there, which the file convention sidesteps entirely.

## Recommendation

**Transport A — reuse `escalations_dir`, hook polls for its own response
file.** This repo already made the "boring beats fast" call for every other
escalation in the system (`escalation.py`'s docstring is explicit about this
trade-off), and a `PreToolUse` gate is rare enough — most tool calls are not
permission-sensitive — that polling latency on the gated path is a non-issue
relative to the human answering it. It also means a live permission request
shows up in exactly the same place (`escalations_dir`, `status` output, the
run log) an operator already watches for every other kind of escalation,
rather than requiring a second surface. Transport B is the better choice only
if hook-call volume or latency sensitivity turns out to be much higher than
expected in practice — worth revisiting then, not now.

## Follow-on units a future implementation plan would need

This ADR does not implement any of the following; it exists so that plan can
skip re-litigating the transport choice.

1. **A new `EscalationKind`** (e.g. `PERMISSION_REQUEST`) added to
   `orchestrator/model.py:165-182`, plus its tier placement in
   `escalation.py`'s `_ON_FAILURE`/`_ON_STUCK`/`_INTERACTIVE` matrices — almost
   certainly `_INTERACTIVE`-or-higher only, since a live tool-call gate is a
   stronger, faster-paced ask of the operator than the existing report-then-
   resume kinds.
2. **The hook script itself** (`hooks/scripts/pretooluse_permission_gate.py` or
   similar) — reads the tool call from stdin, decides whether it needs a live
   escalation (vs. the existing static allowlist handling most calls
   silently), writes its request file into `escalations_dir`, blocks on the
   response file, and translates the operator's `HumanAction` into the
   hook's allow/deny/ask exit contract.
3. **Registration in both `hooks/hooks.json` and `.claude/settings.json`**,
   per this repo's dual-registration rule (see `CLAUDE.md`) — a `PreToolUse`
   matcher (likely scoped to `Bash`, mirroring the existing `PostToolUse.Bash`
   entry) added identically to both files, with `${CLAUDE_PLUGIN_ROOT}` in the
   former and `$CLAUDE_PROJECT_DIR` in the latter.
4. **The broker-side listener/handler** — the request-file watcher described
   under Transport A, most naturally a small background task on the run's
   event loop (alongside the existing per-group coroutines) that turns a new
   `PERMISSION_REQUEST` file into a normal operator-facing escalation and
   writes the response file once answered, reusing `EscalationRequest`/
   `EscalationResponse` and `atomic_write_text` as-is.

## Non-goals of this ADR

This document does not change any code in `orchestrator/` or `hooks/`. It
does not pick a specific set of tool calls to gate, does not design the
static-allowlist-vs-live-escalation decision boundary inside the future hook
script, and does not estimate latency or cost impact — those belong to the
implementation plan this ADR is meant to unblock.
