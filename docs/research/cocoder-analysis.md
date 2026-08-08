# CoCoder Analysis

Repo: https://github.com/Flitternie/CoCoder (cloned to
`/tmp/claude-1001/-home-gbm1996-wksp-smart-mcps/dc3b5753-72c0-4b2a-9f4b-eb3d7312ad95/scratchpad/cocoder`)
Paper: "When Parallelism Pays Off: Cohesion-Aware Task Partitioning for Multi-Agent Coding" (arXiv:2606.00953)

All file paths below are relative to the clone root unless given absolute.

______________________________________________________________________

## 1. Repo overview

- **Language**: Python 3.10+ (targets 3.12 via conda). No other languages in the
  implementation itself.
- **Size**: The actual tool (`code_team/`) is **~13,818 LOC** across ~90 `.py`
  files. The repo also ships `datasets/DevEval` (11 dirs) and
  `datasets/CodeProjectEval` (19 dirs) — real open-source target projects
  (cookiecutter, bplustree, simpy, pyjwt, flask, etc.) used as benchmark
  fixtures, not part of CoCoder's own code. Including those vendored projects
  the repo is ~113k Python LOC total, but that is benchmark data, not the tool.
- **Key entry points** (all invoked as `python -m <module> run --dataset D --repo R`):
  - `code_team/codebase/__main__.py` — sequential single-agent baseline
  - `code_team/parallelbase/__main__.py` — one-agent-per-file baseline
  - `code_team/cohesionbase/__main__.py` — **the CoCoder pipeline** (paper's contribution)
  - `code_team/ribgensim/__main__.py` — standalone RIB (dependency-graph) generator
  - `code_team/claude_code_agent_team/__main__.py` — external baseline that shells
    out to the real `claude` CLI through a local LiteLLM proxy
- **Dependencies** (`code_team/requirements.txt`): `openhands-sdk`/`openhands-tools`
  1.11.4 (the agent runtime/tool framework), `pydantic`, `python-dotenv`,
  `filelock`, `pytest` + plugins. Graph libraries (`networkx`, `python-louvain`
  aka `community`, `python-igraph`, `leidenalg`, `infomap`) are imported lazily
  inside `code_team/cohesionbase/partition/{common,clustering}.py` but are
  **not listed in requirements.txt** — an environment gap; you must
  `pip install networkx python-louvain python-igraph leidenalg infomap` yourself
  for the InfoMap/Leiden code paths to import successfully.
- **License**: Apache License 2.0 (`LICENSE`, 190 lines, standard Apache-2.0 text).
- **Maintenance signal**: the git history is **2 commits total** ("feat: init",
  "feat: init dataset", both same day) — this is a fresh, single-shot paper
  code drop, not a project with iterative maintenance history. Treat it as a
  research artifact / reference implementation rather than a battle-tested
  library. It is runnable (clear Quick Start in `README.md:56-105`) but depends
  on the not-yet-released/attributed `openhands-sdk` pinned exactly at
  `1.11.4`, plus LLM API credentials.
- Config lives in `code_team/common/config.py` (`RuntimeConfig`, `RunConfig`,
  `IterationConfig` dataclasses, config.py:19-38) and `.env`
  (`LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL`, `RIB_DEP_MODEL`, `TEST_PYTHON_PATH`).

______________________________________________________________________

## 2. Dependency graph construction

**Critical finding**: CoCoder's benchmark task is *whole-repository code
generation from a spec* (PRD + UML diagrams), not modification of an existing
codebase. Consequently its "dependency graph" is **not built by static
analysis of real code** in the live pipeline — it is authored by an LLM sub-agent
before any source file exists.

- The graph is called a **RIB** ("Repository Interface Blueprint"): a JSON list
  of modules, each containing files, and each file containing `classes`
  (with methods/parameters), top-level `functions`, `global_code`, a free-text
  `description`, and a `dependencies: [file_path, ...]` list.
- Generation: `code_team/codebase/tools/generate_architecture.py:56-145`
  (`GenerateArchitectureExecutor`) builds a prompt from the PRD/UML/architecture
  docs and calls `self.runtime.run_agent("architecture", prompt, ...)` — an LLM
  sub-agent free-writes the RIB JSON. The variant with dependency edges is
  `code_team/parallelbase/tools/generate_architecture_dep.py:29-108`, using
  `architecture_prompt_dep` (`common/prompts.py`), invoked via the CLI flag
  `--rib-dep-tool` (see `README.md:99-105`).
  There is also `code_team/ribgensim/run.py` — a standalone driver
  (`process_project`, ribgensim/run.py:113-202) that runs the same
  generation step in isolation and saves `rib.json`, used to pre-compute RIBs
  for reuse across pipeline runs (`--rib-file` in the Quick Start).
- **Nodes = files** (not individual symbols). Symbols (classes/functions/params)
  are attributes *of* a file node, used later for weighting, not separate graph
  nodes.
- **Edges = file->file `dependencies` entries**, extracted generically by
  `extract_edges()` in `code_team/cohesionbase/partition/common.py:26-29`
  (drops self-loops and dangling references).
- **Edge weights**: `code_team/cohesionbase/partition/w_rib_cosine.py:71-82`
  (`weights_rib_cosine`). For every RIB-declared dependency edge `(source, target)`, both files get a symbol-count vector (`_build_symbol_vector`,
  w_rib_cosine.py:25-56: class names ×2, function names ×1 \[skipping
  dunders\], `global_code` names ×2, and non-builtin parameter *type*
  annotations ×1 — types-you-reference share the same key space as
  types-you-define, so cosine similarity captures "A defines Node, B takes a
  Node parameter"). Cosine similarity is scaled `sim*10` (no
  cross-project max-normalization) or falls back to a flat `default_weight=0.1`
  if the two files share no symbols at all (w_rib_cosine.py:79-82). An
  almost byte-identical `w_ssat_v3_cosine.py` computes the same thing over the
  "SSAT" architecture format used by the sequential (`codebase`) pipeline's
  two-phase skeleton flow — SSAT and RIB are sibling JSON schemas for two
  different pipelines, not two stages of one pipeline.
- **No real static analysis is wired into the live pipeline.** There IS a
  proper Python AST-based extractor —
  `code_team/common/utils/api_extract.py` (`extract_api`, lines 6-33, plus
  `get_signature`/`get_return_type`, using stdlib `ast`) — but a repo-wide grep
  shows **it is never imported anywhere else in `code_team/`**. It's dead code
  (or a leftover hook for a "verify generated code matches RIB" step that
  isn't wired up), not the graph source.

______________________________________________________________________

## 3. Hub detection and partitioning

Three-stage pipeline, run by the single composite tool
`code_team/cohesionbase/tools/partition_into_groups.py:139-267`
(`PartitionIntoGroupsExecutor.__call__`):

1. **Structural hub isolation** — `detect_roles()`,
   `code_team/cohesionbase/partition/common.py:43-63`. Pure in/out-degree
   thresholding on the dependency graph (no ML): for each non-`__init__.py`
   file `f`, `fan_in = |depends_of[f]| / (n-1)`, `fan_out = |depended_by[f]| / (n-1)`.
   If `fan_in > threshold` -> **out_hub** (widely-depended-upon /
   top-level aggregator terminology is inverted from what you'd expect —
   read the code, not just the docstring: `roles[f]="out_hub"` when *many
   things depend on f*, i.e. what the paper calls an "in-hub" utility, is
   labeled `out_hub` in this function; `role_grouping()` in
   `post_processing.py:13-33` uses the same variable naming inversion
   consistently, so it's internally consistent but the identifier names are
   swapped relative to the docstring — a real gotcha if you port this code).
   `ROLE_THRESHOLD = 0.4` is a module constant in
   `partition_into_groups.py:37`, so ~40% of all other files must
   depend on/be depended-by a file for it to be pulled out as a hub.
   Hub files are removed before community detection, then reattached as
   singleton groups (`role_grouping`, `post_processing.py:13-33`;
   `attach_init_files` additionally hangs `__init__.py` files onto whichever
   group they connect to, `post_processing.py:212-292`, purely as bookkeeping —
   they never influence the partitioning decision itself).
1. **Community detection**: **InfoMap** is the algorithm actually wired into
   the live tool (`infomap_partition`,
   `code_team/cohesionbase/partition/clustering.py:25-47`, using the `infomap`
   PyPI package, `directed=True`, minimizes description length of a random
   walk — Rosvall & Bergstrom 2008). It is applied only to the "core" (non-hub)
   subgraph (`role_grouping(rib, weights, infomap_partition, threshold=...)`,
   called from `partition_into_groups.py:165`). **Directed Louvain**
   (`clustering.py:8-22`, `networkx.community.louvain_communities`) and
   **Leiden** (`clustering.py:50-73`, via `igraph`+`leidenalg`,
   `RBConfigurationVertexPartition`) are implemented as swappable alternatives
   but are **not called by the live tool** — only InfoMap is used in
   `partition_into_groups.py`. An undirected Louvain (`louvain_partition`,
   `common.py:68-78`, via the `python-louvain` `community` package) is also
   present and used only by the dead evaluation harness in `common.py` (see below).
1. **Latent parallelism exploitation ("lift")** —
   `lift_independent()`, `code_team/cohesionbase/partition/post_processing.py:36-105`.
   Within each InfoMap community, splits off files that share a parent/hub but
   have no direct dependency edge between them (siblings that only depend on
   a shared internal hub, or files with no internal deps at all get split by
   connected components, `post_processing.py:64-82`). This is how independent
   leaf files get pulled into their own single-file group rather than being
   stuck serialized behind unrelated siblings in the same InfoMap cluster.

**Granularity / size control**:

- `ROLE_THRESHOLD` (`partition_into_groups.py:37`, default 0.4) controls how
  aggressively hub files are carved out before clustering.
- InfoMap itself has no explicit resolution knob exposed here (parameterless
  call, `clustering.py:38-42`); Louvain/Leiden alternatives do expose
  `resolution` but are unused in the live path.
- **Group-size bound is enforced *after* partitioning**, not during: `merge_small_groups()`,
  `post_processing.py:295-401`, greedily merges small groups (`max_group_size: int = 8`, default) **bottom-up along dependency edges only** (a group can
  only merge into a group it depends on — `post_processing.py:361-367`), and
  **only if the merge doesn't increase a simulated zero-communication
  makespan** (`_simulate_zero_comm_makespan`, `post_processing.py:146-187`,
  a topological-order greedy scheduler simulation using RIB symbol-count as a
  proxy for "work", `_estimate_file_work`, `post_processing.py:132-143`).
  This merge step is **gated by an env var and off by default**:
  `use_merged = os.environ.get("ENABLE_MERGE_GROUPS", "0") == "1"` at
  `partition_into_groups.py:195`. Without it, group count/size is whatever
  InfoMap+lift produces, unbounded.
- **Partition quality metric (MQ)** — `_compute_mq()`,
  `partition_into_groups.py:83-94`: `(intra_weight - inter_weight) / total_weight` over the cosine-weighted edges; reported to the leader agent
  but not used as an optimization objective by the algorithm itself (it's
  diagnostic output only).
- `code_team/cohesionbase/partition/common.py` also contains a large amount of
  **dead research/eval scaffolding** unrelated to the live pipeline:
  `load_project`, `GROUND_TRUTH` (hand-labeled ground-truth partitions for 4
  specific projects: bplustree, cookiecutter, imapclient, simpy —
  `common.py:127-172`), `rand_index`, `evaluate` — these reference a
  `DATA_DIR = .../partition/data` directory that **does not exist in this
  checkout** and are exported from `partition/__init__.py` but never imported
  by any other module (`grep -rl load_project code_team` returns only
  `common.py` and `__init__.py`). This is standalone algorithm-tuning code
  the authors used to validate InfoMap against a small hand-labeled gold set,
  not part of the runtime.

______________________________________________________________________

## 4. Task-to-partition mapping

There is **no separate "map an incoming task to a partition" step** in the
sense of an LLM or embedding-based router. Because the benchmark is
generate-the-whole-repo-from-a-spec, the mapping is fully structural and
happens as a side effect of partitioning:

- After partitioning, `_build_output()` (`partition_into_groups.py:177-190`)
  builds a `file_to_group: dict[file_path -> group_name]` — a pure lookup
  table, no LLM call.
- `_auto_init_and_spawn()` (`partition_into_groups.py:269-343`) then converts
  every RIB file into a scheduler task whose `owner` is `file_to_group[path]`
  (`partition_into_groups.py:288-295`) and whose `deps` are literally the
  RIB's `dependencies` list for that file (`file_deps`, built at
  `partition_into_groups.py:280-284` from `flatten_files(rib)`). "Task" and
  "file" are the same unit throughout — there's no coarser task abstraction
  layered on top, and no embedding/semantic matching is involved anywhere in
  this repo.
- If your orchestrator instead starts from a human-written feature request
  that must be *routed to* one or more existing partitions (rather than
  "every file is a task"), CoCoder has no analog to reuse — you'd need to add
  that layer yourself (e.g. LLM classification of the request against
  `groups_output[*].files` + descriptions, which the RIB already carries per
  file at `f.get("description")`).

______________________________________________________________________

## 5. Scheduler

Dependency-aware, **not a single topological sort computed up front** — it's
a live, reactive state machine plus a polling wake-up loop:

- **State machine**: `code_team/cohesionbase/tools/shared_task_list.py`.
  Tasks have status `pending -> ready -> in_progress -> completed`
  (constants at `shared_task_list.py:64-67`). `_cmd_init`
  (`shared_task_list.py:151-225`) builds the reverse-dependency `blocks` map
  and marks deps-free tasks `ready` immediately; it **refuses to
  re-initialize** an existing task list (`shared_task_list.py:157-165`) — a
  single source of truth for the whole run. `_cmd_claim`
  (`shared_task_list.py:228-272`) is idempotent (re-claiming your own
  in-progress task returns success, not an error) and reports `blocked_by`
  when deps aren't met. `_cmd_complete`
  (`shared_task_list.py:275-317`) marks the task done and calls
  `_update_readiness_for()` (`shared_task_list.py:121-135`) which does a
  **targeted** reverse-edge (`blocks`) scan rather than rescanning the whole
  graph, then reports `newly_ready` tasks back to the caller (own agent) and
  in `all_done`. Concurrency is handled with a **file lock**
  (`_TaskListLock` / `_acquire_lock`, `shared_task_list.py:73-119`, exclusive
  file-create with retry + stale-lock override) — not a DB, not an in-memory
  mutex shared across processes (all agents are threads in one process here,
  but the file-lock design would also work across processes).
- **Wake-up**: a background thread,
  `_task_list_poller()` in `code_team/parallelbase/orchestrator.py:429-500`,
  polls `task_list.json` every 0.5s (idle) / 5s (after just notifying someone)
  and pushes a message to the task's `owner` agent when a task goes
  `ready`+unclaimed (`orchestrator.py:454-464`), or reminds a paused agent
  about its still-`in_progress` task (`orchestrator.py:478-493`). Agents do
  not poll themselves; they get pushed to.
- **On failure / stuck states**: two independent guardrails, both in
  `parallelbase/orchestrator.py`:
  - **Deadlock monitor** (`_deadlock_monitor`, `orchestrator.py:370-423`): if
    the message bus is empty, no futures in flight, and no task is
    `ready`/`in_progress` for `DEADLOCK_TIMEOUT_SEC=15s`
    (`orchestrator.py:34`) straight, it wakes the **leader** with a diagnostic
    system message (`_wake_leader_on_deadlock`, `orchestrator.py:313-345`).
    Capped at `MAX_DEADLOCK_WAKEUPS=3` (`orchestrator.py:35`); beyond that it
    force-terminates the run (`orchestrator.py:415-421`).
  - **Text-only-exit nudge** (`_agent_should_continue` /
    `_run_agent`, `orchestrator.py:548-623`): if an agent's turn ends with
    plain text instead of a tool call (LLM forgot to call the next tool), the
    orchestrator auto-injects a "resume your workflow" system message and
    re-runs, up to 3 nudges.
  - There is **no re-partitioning / re-scheduling** on file-level failure —
    failures are handled entirely in **Phase 4** by the leader directly
    patching code and re-running tests (see prompts.py Phase 4,
    `code_team/cohesionbase/prompts.py:110-118`), capped at `testfix_iters`
    (CLI-configurable, default 10). If tests still fail after that many
    rounds, the leader force-finishes (`prompts.py:116, 138`). There's no
    mechanism to re-cluster the dependency graph or reassign files to
    different group agents mid-run — the partition computed once at Phase 2
    is fixed for the whole run.
- **Cyclic dependencies**: `_simulate_zero_comm_makespan`
  (`post_processing.py:146-187`) explicitly documents that some analyzed
  projects contain import cycles and appends unresolved cyclic leftovers to
  the topo order rather than failing (`post_processing.py:173-175`) — but note
  `shared_task_list`'s `_cmd_init` has **no cycle detection at all**; a real
  dependency cycle in the RIB would leave those tasks permanently `pending`
  (never satisfying "all deps completed"), relying entirely on the deadlock
  monitor to notice and escalate to the leader rather than any structural fix.

______________________________________________________________________

## 6. Agent communication

- **Framework**: **OpenHands SDK** (`openhands-sdk`/`openhands-tools`, pinned
  `1.11.4`) — an `Agent`/`LocalConversation`/`ToolDefinition` framework, driven
  via LiteLLM for model routing (any LiteLLM-supported provider: OpenAI,
  Anthropic, Bedrock, Azure, Portkey). This is **not** the Claude Code CLI in
  the main `cohesionbase`/`parallelbase`/`codebase` pipelines — those spawn
  in-process `Agent` objects with custom tool sets
  (`code_team/cohesionbase/agent_factory.py:222-265`,
  `create_agent`). The **separate** `claude_code_agent_team/` pipeline *does*
  shell out to the real `claude` CLI as a subprocess through a local LiteLLM
  proxy (`code_team/claude_code_agent_team/README.md:1-72`,
  `start_proxy.sh`) — that's the one directly analogous to your Claude Code
  orchestrator, but it's a thin external baseline, not where the
  partitioning/scheduling logic lives.
- **Orchestrator = pure message router + lifecycle manager**, explicitly by
  design (`code_team/parallelbase/orchestrator.py:1-5` docstring: "does not
  maintain any phase state ... all business logic lives in the Leader agent's
  LLM context"). One thread per delivery via `ThreadPoolExecutor`
  (`orchestrator.py:85`), one `threading.Lock` (`run_lock`) per agent to
  serialize `conversation.run()` calls (`AgentHandle`, `orchestrator.py:42-48`).
- **Inter-agent transport**: `AgentMessage(from_agent, to_agent, content, timestamp)` dataclass (`code_team/parallelbase/message_bus.py:19-25`) over a
  thread-safe `queue.Queue`-backed `MessageBus` (`message_bus.py:28-47`).
  Plain free-text `content` — there's no structured task-spec schema for
  agent-to-agent chat (unlike the task list, which *is* structured). Sending
  is exposed to agents as the `send_to_agent` tool
  (`code_team/parallelbase/tools/send_to_agent.py:24-79`, modes
  `"direct"`/`"broadcast:<role-prefix>"`) and agent lifecycle as
  `agent_manager` (`spawn`/`dismiss`/`query`,
  `code_team/parallelbase/tools/agent_manager.py:29-133`).
- **"Task spec in"**: group agents do **not** receive their file list as a
  free-text prompt only — `read_rib` (`code_team/cohesionbase/tools/read_rib.py:64-98`)
  is a deterministic (no-LLM) tool that, given a target file path, returns
  (a) the exact RIB JSON entry for that file (classes/functions/params/
  descriptions, i.e. the structured spec) and (b) a lightweight
  `{path, description}` summary of every other file for cross-file context.
  This guarantees the group agent always sees the ground-truth interface spec
  regardless of what the leader typed into the initial message
  (design rationale in `read_rib.py:1-9`).
- **"Reports out"**: also plain text over `send_to_agent`, by convention
  (e.g. group agents must literally call
  `send_to_agent(mode="direct", target="leader", message="Group done")` —
  `code_team/cohesionbase/prompts.py:232-234`). There's a hard rule baked into
  the system prompt that **plain-text replies are invisible to everyone**
  (`prompts.py:236-243`) — only tool calls are delivered, which is a load-
  bearing constraint of the whole message-bus design (silent text exit =
  the agent thinks it reported, but nobody heard it — this is exactly what
  the deadlock monitor and text-only-exit nudge in orchestrator.py exist to
  paper over).
- **Reviewer/verification loop**: **two separate LLM-judge loops**, both
  single-shot `llm.completion()` calls (not full agentic loops):
  1. **Architecture-level**: `judge_architecture`
     (`code_team/codebase/tools/judge_architecture.py:58-114`) scores the RIB
     1-10 across "requirement coverage, module partitioning, interface
     consistency, dependency management"; leader must re-edit and re-judge
     while score < 8, capped at 3 attempts (enforced only via system-prompt
     instruction, `prompts.py:91-95`, not code — the tool itself doesn't
     track attempt count).
  1. **Test-level**: no LLM judge — ground truth is literally running
     `pytest` via `run_tests`/`ParallelRunTestsTool`
     (`code_team/parallelbase/tools/parallel_run_tests.py:77-154`), parsed
     with `_parse_pytest_counts`. The leader repairs code directly against
     failing test output, capped at `testfix_iters` (default 10,
     `prompts.py:110-118`).
     There's also a **file-skeleton judge** (`judge_file_skeleton.py`) and
     **RIB-vs-skeleton judge** (`judge_rib.py`) used only in the sequential
     `codebase`/`parallelbase` pipelines' two-phase (skeleton-then-code) flow —
     cohesionbase skips the skeleton phase entirely (agent_factory.py:6-9
     docstring: "Removes judge_rib (no global skeleton phase)").

______________________________________________________________________

## 7. Reusability assessment

**Clean, reusable as a library (pure Python, no OpenHands/LLM dependency at
import time)**:

- `code_team/cohesionbase/partition/common.py` — `extract_files`,
  `extract_edges`, `detect_roles`, `louvain_partition`,
  `partition_to_groups` (generic dict/list manipulation).
- `code_team/cohesionbase/partition/clustering.py` — `directed_louvain`,
  `infomap_partition`, `leiden_partition` (each takes `(weights: dict[(str,str) -> float], nodes: set[str], **kwargs) -> dict[str,int]` — a completely
  generic weighted-directed-graph-partitioning interface, no RIB/JSON
  coupling at all).
- `code_team/cohesionbase/partition/post_processing.py` — `role_grouping`,
  `lift_independent`, `attach_init_files`, `merge_small_groups` all operate on
  `partition: dict[file_path, group_id]` + `rib: list[dict]` (only using
  `extract_edges`/`extract_files` from `common.py`) + a generic weight dict.
  These would run unchanged on graph data from any source that can be shaped
  into the same `(files-with-symbol-lists, dependency-edges)` structure.
- `code_team/cohesionbase/partition/w_rib_cosine.py` — the symbol-cosine
  weighting is a pure function of `{classes, functions, global_code, parameters}` dicts; it doesn't care where those dicts came from.

**The separation between "graph layer" and "partition layer" is clean at the
function-signature level** (weights and edges are plain Python
dicts/tuples/sets throughout — no RIB-specific types leak into
`clustering.py` or the merge/lift logic in `post_processing.py`), **but not
clean at the data-shape level**: every one of these functions is called with
data first shaped by `extract_files`/`extract_edges`
(`code_team/cohesionbase/partition/common.py:18-29`), which expects the exact
RIB JSON schema (`{"files": [{"path", "dependencies", "classes", "functions", "global_code"}]}`) — i.e. **the "external dependency graph adapter" doesn't
exist as a separate seam; RIB-shape parsing is inlined into the same module
as the partitioning algorithms.** To run this on top of codegraph's own
symbol graph (callers/callees/impact-analysis) instead of CoCoder's
LLM-authored RIB, you would:

1. Write a small adapter that walks codegraph's file/symbol graph and
   produces (a) a `files: dict[path -> {classes, functions, global_code}]`-
   shaped structure (or just monkey-patch `extract_files`/`extract_edges` to
   read from codegraph's index instead of a RIB JSON) and (b) a
   `dependencies: list[str]` per file (codegraph's caller/callee edges
   collapsed file's-worth-of-references-cross-file, i.e. roughly what
   `mcp__codegraph__impact` already computes per-symbol — you'd aggregate to
   file granularity).
1. Everything downstream — `detect_roles`, `infomap_partition` (swap in
   directed_louvain/leiden if InfoMap's extra dependency is unwanted),
   `lift_independent`, `merge_small_groups`, `attach_init_files` — would run
   as-is with **zero changes**, since they only consume the generic
   `weights`/`edges`/`partition` dict shapes, never the RIB JSON directly
   (aside from `extract_edges`/`extract_files` calls sprinkled through
   `lift_independent`/`merge_small_groups`/`attach_init_files`, which you'd
   redirect to your adapter's output instead of a literal RIB file).
1. The cosine weighting in `w_rib_cosine.py` is the one place worth
   reconsidering rather than reusing verbatim: with codegraph you have *real*
   call/reference edges (from `mcp__codegraph__impact`/callers-callees), which
   is strictly better ground truth than "symbol-name cosine similarity" — a
   proxy the paper needed only because, at RIB-authoring time, no code exists
   yet to analyze for real references. With a real graph you'd likely want to
   weight edges directly by reference count/impact-radius rather than
   resurrecting the cosine-over-symbol-names heuristic.

**Entangled with the OpenHands agent framework / benchmark harness (do NOT
try to reuse as-is)**:

- `code_team/cohesionbase/tools/partition_into_groups.py` — the
  `PartitionIntoGroupsExecutor` (partition_into_groups.py:130-268) is a
  `ToolExecutor` glued to OpenHands `Action`/`Observation` Pydantic models,
  and its `_auto_init_and_spawn` (partition_into_groups.py:269-343) directly
  calls `self.orchestrator.spawn_agent(...)` and
  `self.message_bus.send(AgentMessage(...))` — i.e. the partitioning call and
  the "spawn one coder agent per partition" call are fused into a single tool,
  not two composable steps. You'd want to fork this file and split "compute
  partition" from "spawn agents for partition", keeping only the former.
- `code_team/cohesionbase/tools/shared_task_list.py` (the scheduler) is
  reusable *design* (dependency state machine + file lock) but is wired as an
  OpenHands tool with `conversation._agent_name` resolution
  (`shared_task_list.py:396-401`) — trivial to extract to a plain class, but
  as shipped it's not importable without the SDK types.
- `code_team/parallelbase/orchestrator.py` (message routing, deadlock
  detection, agent nudging) is entirely built around OpenHands
  `LocalConversation`/`ConversationExecutionStatus`/`ActionEvent` types —
  this is the piece most specific to their agent runtime and least portable
  to a Claude-Code-native orchestrator (Claude Code's own subagent/Task tool
  model is a different execution substrate entirely).
- All prompt-construction code (`cohesionbase/prompts.py`,
  `common/prompts.py`, `common/prompt_constants.py`) is tightly coupled to
  their benchmark's artifacts (PRD/UML/architecture-design docs,
  `check_tests/` directory conventions, RIB JSON schema) — useful as *prose
  inspiration* for your own system/task prompts (especially the "Message
  Delivery Rule" and "Implementation Integrity Rules" sections,
  `prompts.py:20-44, 236-243`), not as reusable code.

**Bottom line**: the graph-partitioning core (hub detection -> InfoMap ->
lift -> merge, `partition/common.py` + `partition/clustering.py` +
`partition/post_processing.py`, roughly 600 LOC total) is a genuinely
reusable, dependency-graph-agnostic library once you write one small adapter
converting codegraph's symbol graph into their generic
`{path: {classes, functions, global_code}}` + `dependencies` shape. Everything
above the partitioning call (RIB generation, agent spawning, message bus,
orchestrator, prompts) is benchmark/OpenHands-specific scaffolding you would
not carry over.

______________________________________________________________________

## 8. Key takeaways for the orchestrator design

1. **Separate "compute partition" from "spawn/execute partition" as two
   distinct calls, not one.** CoCoder fuses them in
   `partition_into_groups.py:139-268` (partition -> init task list -> spawn
   agents all in one tool call) for benchmark-latency reasons (comment at
   `partition_into_groups.py:1-10`: "eliminates ~200s of LLM thinking time").
   For your system, keep partitioning pure/inspectable (so a human or a
   review step can see/veto group boundaries before agents are spawned) —
   don't copy this fusion.

1. **Isolate hub files before clustering, or your "vertical" groups will be
   dominated by shared utils.** `detect_roles()`
   (`partition/common.py:43-63`) pulls out high-fan-in/fan-out files first so
   community detection isn't distorted by them; hubs get their own
   single-file groups and are attached to the coder group most likely to own
   them (`role_grouping`, `post_processing.py:13-33`). For service/module
   boundaries this maps directly to: pull out shared libraries/base classes/
   config modules as their own group (or pre-assign them to whichever
   downstream service agent needs them first) before running community
   detection on the rest.

1. **Bound group size only as a post-processing merge step, gated on a
   provable no-regression condition — never as a clustering-time
   constraint.** `merge_small_groups()`
   (`post_processing.py:295-401`) only merges two groups if the merge (a)
   respects dependency direction (can't merge into a group that depends on
   you, `post_processing.py:361-367`), and (b) doesn't worsen a simulated
   parallel makespan (`_simulate_zero_comm_makespan`,
   `post_processing.py:146-187`). This "simulate before merging" pattern —
   don't just cap group size by a hard N, verify the merge doesn't hurt your
   schedule — is directly applicable to bounding your vertical-group sizes.
   Note it's off by default in CoCoder itself
   (`ENABLE_MERGE_GROUPS` env var, `partition_into_groups.py:195`) — worth
   turning on unconditionally in your version, since unbounded InfoMap output
   is exactly the "too many tiny groups" failure mode you're trying to avoid.

1. **The scheduler is a reactive state machine over a locked JSON file, not a
   one-shot topological sort.** `shared_task_list.py` computes readiness
   incrementally on every `complete` call via the reverse `blocks` map
   (`_update_readiness_for`, `shared_task_list.py:121-135`), not by
   recomputing a full topo order. Combined with the poller
   (`orchestrator.py:429-500`) that pushes wake-ups to owners rather than
   agents pulling, this gives you dependency-aware scheduling *without* a
   global barrier between waves — a group agent can start its 3rd file the
   moment that file's deps clear, even while sibling groups are still on
   their 1st file. Worth replicating literally (it's a small, clean, reusable
   file-lock-based design) rather than reinventing.

1. **Plan for the "the coder said it's done but nobody heard it" failure
   mode explicitly — it's structural, not incidental.** Because reports only
   count if delivered via a tool call (`prompts.py:236-243`), CoCoder needed
   *two* independent watchdogs to route around it: a deadlock monitor
   (`orchestrator.py:370-423`, wakes leader after 15s of total silence, capped
   at 3 wakeups) and a text-only-exit nudge
   (`orchestrator.py:548-623`, re-prompts an agent that stopped without
   calling a tool, up to 3 times). If your orchestrator lets subagents "just
   finish" without a mandatory structured completion signal, budget for the
   equivalent of both watchdogs from day one rather than discovering the gap
   in production.

1. **A reviewer loop only pays for itself at the plan/architecture level, not
   per-file.** CoCoder LLM-judges the RIB before any code is written
   (`judge_architecture.py:58-114`, threshold 8/10, max 3 attempts) but
   deliberately has **no LLM judge for individual generated files** in the
   cohesionbase pipeline — correctness is checked only by compiling
   (`compile_check`) and, at the very end, running the real test suite
   (`run_tests`), with the leader repairing failures directly
   (`prompts.py:110-118`, `parallel_run_tests.py`). For your reviewer loop,
   consider putting the expensive judge pass at plan/partition time (does
   this decomposition make sense before we fan out?) rather than gating every
   coder's output — cheaper and, per their design, apparently sufficient.

1. **Give every coder agent a structured, deterministic "task spec in" tool —
   don't rely on the leader to hand-copy the spec into a chat message.**
   `read_rib.py:64-98` is a zero-LLM, pure file-lookup tool that returns the
   exact interface contract for one file plus a lightweight index of
   everything else. This guarantees consistency regardless of what the
   dispatching agent typed, and is directly portable to your design: give
   each vertical-group coder a tool that deterministically re-fetches "your
   slice of the plan + a summary of everyone else's slice" from disk/index,
   rather than trusting the orchestrator's free-text handoff message alone.

1. **Watch the "self-inconsistent naming" trap when you port hub-detection
   code.** `detect_roles()` (`partition/common.py:43-63`) labels a file
   `"out_hub"` when *many other files depend on it* (i.e., what most people
   would intuitively call an in-hub/widely-used-utility) and `"in_hub"` when
   it depends on many others (a top-level aggregator/entrypoint) — the
   opposite of the natural reading and of the paper's own prose
   ("*in-hubs* (widely depended-upon utilities) and *out-hubs* (top-level
   aggregators)", `README.md:76-82`). The code is internally consistent (every
   caller of `detect_roles` respects its own labels), but if you copy this
   function and reason about it using the README's terminology instead of
   reading the code, you will build the reverse of what you intend. Verify
   against actual behavior, not variable names or docstrings, when reusing
   this specific function.
