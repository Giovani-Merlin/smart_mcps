# 0011 — A `run` child outlives the orchestrator and is re-adopted on resume

**Context.** Every process the orchestrator spawns today is a worker `claude`
recorded in `state.json` `live_pids`, and `Scheduler._reap_orphans` kills each
one on `resume` — cheap, because Warm Resume keeps the session. A `run`
\[[Unit Recipe]\]'s command is a render or a judge pass that can take an hour; B7
lost three generations on one render, and killing it on every orchestrator
crash or laptop suspend reproduces that.

**Decision.** A \[[Run Child]\] is launched through a wrapper that writes its exit
status to a file, in its own process group, with output to files rather than
pipes, and is recorded per attempt under the group's directory — never in
`live_pids`, so the reaper never sees it. On resume the `run` executor re-adopts
a child that is still the same process (kernel start time + argv[0], the
identity check `LivePid` already uses), keeps polling it, and reads the exit
file when it ends; the wall-clock cap counts *awake* time — `CLOCK_MONOTONIC`
from launch, which excludes suspend and is comparable across an orchestrator
restart within one boot. Only an unclean death leaves a child to re-adopt: a
deliberate stop (Ctrl-C, Stop) kills its process group.

**Why.** The alternative, kill-and-relaunch, is simpler and keeps "no process
outlives its orchestrator" true, but it re-pays the whole command after every
crash and lets a relaunch append to a half-written `data_dirs` output. The
price accepted: the orchestrator now owns a class of process it did not fork
in this lifetime, and the attempt directory format is state a later version
must keep reading.
