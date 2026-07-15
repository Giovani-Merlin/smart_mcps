<img src="https://r2cdn.perplexity.ai/pplx-full-logo-primary-dark%402x.png" style="height:64px;margin-right:32px"/>

# I am designing an execution architecture for Claude Code, initially as skills/subagents/plugins plus shell orchestration, possibly later with an external launcher script that still uses my Claude subscription rather than the API. I need a deep technical research report with concrete recommendations, examples, and tradeoffs.

My target workflow is this:
I start from a large plan/spec document for a software project.
A “plan grouper” agent/skill reorganizes the plan into vertical execution groups.
Grouping priority is internal service boundaries first, not user-visible slices first.
Each group must remain small, testable, and observable, and should include:
implementation tasks,
function/unit tests where appropriate,
integration/verification steps,
acceptance criteria,
explicit dependencies on other groups.

Each group should be executed by a coder subagent in isolation, ideally in its own worktree from the start.
Independent groups should run in parallel; dependent ones should wait.
Each coder subagent owns its own gather → implement → verify loop.
The orchestrator should be lightweight: mainly dependency scheduling, progress tracking, and adaptation when a subagent reports something surprising.
If reality changes during execution, the orchestrator should be allowed to rewrite unfinished groups and downstream dependency graph, not just continue rigidly.
I care a lot about context efficiency, avoiding monolithic sessions, reducing overflow, and reusing shared context across sibling subagents.
I specifically want research and recommendations on these questions:
A. Task grouping / decomposition
What are the best scientific and practical approaches for grouping software-engineering tasks into execution units for LLM agents?
How should I think about granularity so that groups are:
small enough to fit context safely,
large enough to preserve coherence and avoid coordination overhead,
observable and testable,
aligned to internal service boundaries?

Are there papers, benchmarks, or engineering writeups on decomposition quality, decomposition granularity, observability, or success rate in coding agents / multi-agent systems?
I currently think a forced split should happen based on an estimated difficulty level of the group rather than simplistic file-count thresholds. Please evaluate whether that is defensible and suggest how “difficulty” could be operationalized.
B. Shared context / prompt reuse
I want sibling subagents to share as much stable context as possible.
Compare these approaches:
forked sessions / inherited context,
a shared base document or compiled project context injected into every subagent,
prompt-prefix caching / stable prompt head with variable task tail.

I suspect the best pattern is a stable shared prefix and a reusable base document, not relying too much on fragile live-session inheritance. Please evaluate this scientifically and practically.
I care about cache hit rate, prompt stability, context reuse, and preventing overflow when many similar subagents run in parallel.
Include relevant Claude / Anthropic docs on prompt caching and any good technical analyses of Claude Code or prompt-prefix reuse patterns.
C. Claude Code execution patterns
Find the best current material on:
Claude Code subagents,
custom subagents,
worktree isolation,
parallel execution,
orchestration / agent teams / dynamic workflows,
plugin/skill ecosystems around Claude Code.

I already know Compound Engineering and Claude Superpowers. Please find:
those repos/plugins,
similar or more advanced repos,
advanced-user patterns from GitHub, blogs, or docs,
practical examples of launching multiple Claude Code sessions/subagents with worktrees.

I want recommendations specifically for a setup where planning and execution are separate, and execution is done via multiple focused subagents rather than one monolithic agent.
D. Orchestrator design
I want the orchestrator to be minimal and robust.
It should:
schedule independent groups in parallel,
wait on dependencies,
read the subagent’s final feedback,
update the graph if surprising findings occur,
rewrite unfinished groups/downstream dependencies when needed,
otherwise stay out of the way.

Please recommend whether this is better implemented:
purely as Claude Code skills/subagents,
as Claude Code plus shell/python launcher,
or as a more formal workflow framework.

I do not want recommendations centered on using the Anthropic API directly.
E. Deliverables I want from the research
Please produce:
A recommended architecture for my use case.
A decision framework for task grouping granularity.
A recommended prompt/context design for shared base context and cache-friendly execution.
A shortlist of GitHub repos / plugins / skills / tools most relevant to this setup.
A shortlist of scientific papers most relevant to:
task decomposition,
multi-agent orchestration,
context management,
observability / verification,
coding-agent execution.

A concrete proposal for:
a Plan Grouper skill/prompt,
a Base Context Compiler skill/prompt,
an Execution Orchestrator prompt/skill design.

If possible, give sample schemas for:
group output JSON,
dependency graph representation,
per-group verification checklist,
subagent final report format.

Important preferences and constraints:
Prefer internal service boundaries first when grouping.
Each group must include verification/testing, not just implementation.
Execution should begin with worktree isolation.
The orchestrator may rewrite unfinished work when reality changes.
Optimize for observability and success rate, not just raw parallelism.
Avoid giant groups and avoid excessive over-decomposition.
Focus on practical systems used by advanced users, not generic multi-agent hype.
Prioritize sources from official docs, high-quality technical blogs, GitHub repos, and serious papers.
Please structure the answer as:
Executive recommendation
Scientific grounding
Claude Code / plugin / repo landscape
Recommended architecture
Prompt and schema proposals
Risks / failure modes / tradeoffs

Executive recommendation
------------------------

Design this as a three‑layer system: (1) a Plan Grouper that turns a large spec into small, vertical, service‑aligned execution groups with built‑in verification; (2) a Base Context Compiler that assembles a cache‑friendly, reusable project head plus per‑group tails; and (3) a minimal orchestrator that schedules groups across Claude Code coder subagents, each in its own git worktree, and rewrites unfinished groups when reality changes.[^1][^2]

Use difficulty‑based splitting rather than naïve file counts, grounded in dependency density and change impact, and favor stable shared prefixes plus a compiled base document injected into every subagent over fragile live session inheritance, aligning with Anthropic’s prompt caching guidance and recent Claude Code caching analyses.[^3][^4][^5][^6][^7]

Scientific grounding (task grouping / decomposition)
----------------------------------------------------

Recent multi‑agent coding work treats orchestration as a graph partitioning problem where tasks correspond to code regions and edges capture dependencies or communication cost.  Cohesion‑aware Coder (Co‑Coder) builds static dependency graphs, isolates “hub” files, partitions via community detection, and executes partitions with a dependency‑aware scheduler, improving pass rate up to 14% and wall‑clock speed up to 2.1× while reducing cost versus naive file parallelization and Claude Agent Teams.[^8][^3]

Runtime‑Structured Task Decomposition for agentic coding systems similarly advocates building a repository‑level dependency graph, then cutting it along cohesion boundaries rather than arbitrary file or directory counts, and shows that decomposition quality (cohesion within groups, minimal cross‑group edges) strongly correlates with coding success in DevEval‑style benchmarks.  Complementary work on modular task decomposition and dynamic collaboration shows that hierarchical subtasking with explicit constraint parsing and global consistency checks yields higher success rates and better robustness than flat, one‑shot splits.[^9][^8]

Other agent frameworks (TDAG, utility‑aware decomposition) stress dynamic decomposition with feedback: they decompose into subtasks, but allow later tasks to be rewritten based on partial failures to avoid error propagation, and evaluate agents on partial progress rather than binary success.  This matches your requirement that the orchestrator can rewrite unfinished groups and downstream dependencies when reality changes, rather than executing a static DAG to completion.[^10][^11][^12]

Granularity \& “difficulty” operationalization
---------------------------------------------

From these papers and Claude Code practice, there are clear signals for granularity: groups should be small enough that their spec, code deltas, tests, and logs fit comfortably in a single Claude coder subagent context (including base prefix), but large enough to preserve coherence around an internal service boundary and avoid excessive orchestration overhead.  In Co‑Coder and related work, over‑decomposition (too many tiny slices) increases communication and context transfer cost and can erase parallelism benefits; under‑decomposition creates groups whose critical path dominates total runtime and whose context exceeds practical window sizes.[^3][^9][^8]

Your instinct to force splits based on estimated difficulty rather than file counts is defensible and aligns with these findings: difficulty is more closely tied to dependency degree, semantic coupling, and verification surface than raw file numbers.  A concrete difficulty score per candidate group could combine:[^10][^8][^3]

* **Structural metrics** – number of files, functions, and modules touched; average and max fan‑in/fan‑out of involved nodes in the dependency graph.[^8][^3]
* **Change impact metrics** – presence of hub files (high centrality), cross‑service calls, persistence and auth layers, public API surfaces.[^13][^3]
* **Verification surface** – number of unit tests to update/add, integration paths, external systems mocked, and acceptance criteria items.[^12][^13]
* **Non‑local effects** – migrations across frameworks or shared libraries, feature flags, or cross‑cutting concerns (logging, telemetry).[^8]

You can then set thresholds like “difficulty score > D_max or predicted token budget > T_max ⇒ split group at the highest‑cost edge cut that preserves service boundaries” rather than “> N files ⇒ split.”  This matches utility‑aware decomposition work, which optimizes a trade‑off between computational savings from parallelism and communication/context overhead from splitting, rather than count‑based heuristics.[^11][^3][^8]

Claude Code / plugin / repo landscape
-------------------------------------

Anthropic’s Claude Code docs explicitly distinguish four parallelization modes: subagents, agent view, agent teams, and dynamic workflows.  Subagents are delegated workers inside one session with their own context; agent view is a UI layer for monitoring background sessions; agent teams coordinate multiple sessions with shared task lists; dynamic workflows move orchestration into executable scripts that fan tasks across tens to hundreds of subagents with adversarial cross‑checking.[^2][^14][^15][^16]

Worktrees are first‑class in Claude Code: each session can run in its own git worktree, giving an isolated checkout and preventing parallel sessions from editing the same files, and agent view and subagents can be configured to automatically use separate worktrees.  External guides on worktree isolation and “Efficient Claude Code: Context Parallelism \& Sub‑Agents” show practical patterns you already lean toward: one worktree per task branch, one Claude session per worktree, and subagents with narrow scopes and tool permissions to avoid context creep.[^1][^17][^18][^19]

On plugins and skills, the Compound Engineering plugin from EveryInc provides a Plan → Work → Review → Compound workflow with CLAUDE.md and AGENTS.md scaffolding, emphasizing spec‑first planning, multi‑agent review, documentation of learnings, and repository health.  Jesse Vincent’s Superpowers plugin is a core skills library for Claude Code focused on TDD, debugging, collaboration patterns, and “proven techniques”, and is explicitly designed as an agentic skills framework and methodology rather than generic tools.  There are also third‑party orchestration plugins (e.g., barkain’s workflow orchestration plugin) that provide multi‑step task decomposition and parallel agent execution with native plan mode integration, plus large subagent libraries like wshobson/agents that you can drop into `~/.claude/agents`.[^20][^21][^22][^23][^24][^25][^19]

Prompt caching \& shared context
-------------------------------

Anthropic’s official prompt caching docs describe a mechanism where stable prompt segments (typically system and early user messages) can be marked cacheable so that repeated calls reuse a cached embedding of the prefix, dramatically reducing latency and cost.  The Claude Code‑specific caching documentation and technical blogs emphasize ordering and stability: keep model choice and core behavioral instructions in the system prompt, keep a reusable project “head” (architecture, conventions, key files) in early user messages, and append variable task‑specific tails later.[^4][^5][^6][^7]

Engineering write‑ups from Anthropic engineers and external builders argue that cache safety depends on treating the cached head as immutable or slowly changing, and on not interleaving volatile conversational content within that head.  They show that high cache hit rates come from (a) a stable shared prefix across sessions/subagents, (b) a compiled base context document that can be injected consistently, and (c) avoiding over‑reliance on live session inheritance because each fork captures a snapshot that diverges as the parent keeps talking.[^6][^7][^2]

This matches your suspicion: in practice, forked sessions/inherited context are convenient for short‑lived side tasks, but fragile for long‑running multi‑agent execution, especially once base context grows and gets noisy.  The recommended pattern for Claude Code is: compile a base context document (project architecture, key services, coding conventions, test strategy) once, keep it in docs or memory (agentmemory, AGENTS.md), and then give every subagent a stable prefix referencing that base plus a small variable tail describing its group, letting prompt caching amortize the head across many similar subagents.[^26][^27][^5][^19][^2][^4]

Recommended architecture (planning \& execution)
-----------------------------------------------

Given your constraints and preferences, the architecture that fits best is:

* **Planning session**: A parent Claude Code session running in “plan mode” (Compound Engineering style) with the Plan Grouper and Base Context Compiler skills installed.[^28][^29][^23][^25]
* **Execution graph**: Plan Grouper emits an explicit graph of execution groups, each aligned to an internal service boundary where possible, with implementation tasks, unit/integration tests, verification steps, acceptance criteria, and explicit dependencies on other groups.[^10][^3][^8]
* **Base context artifact**: Base Context Compiler builds a small set of shared artifacts (e.g., `docs/context/base.md`, `docs/services/*.md`) summarizing architecture, key services, entrypoints, and cross‑cutting constraints, plus derived indices (file maps, test suites).[^27][^7][^26][^6]
* **Orchestrator**: A minimal orchestrator, implemented either as a Claude Code agent/skill or as an external Python/shell launcher, reads the group graph, spins up one Claude coder subagent per group in its own worktree, and monitors their gather → implement → verify loop via structured final reports.[^19][^1][^2]

Task grouping / decomposition design
------------------------------------

Plan Grouper should first perform domain‑aware clustering: identify internal services and modules (API layer, auth, payments, search, etc.) and group tasks primarily around these service boundaries before secondary concerns like user‑visible slices.  You can use static analysis (imports, call graphs, configuration files) plus spec keywords to map each requirement to one or more services, then build a dependency graph where nodes are candidate groups and edges represent explicit data or control dependencies.[^13][^3][^10][^8]

Decomposition should proceed in two passes:

1. **Top‑down pass**: From the spec, identify vertical slices within each service (e.g., “add feature flag X to service Y”, “extend endpoint Z with parameter P”) that are independently testable and observable; each slice must include its code changes and associated tests/verification steps.[^30][^12][^13]
2. **Bottom‑up refinement**: Within each slice, compute a difficulty score as above, then split groups only when difficulty or predicted token budget exceed thresholds, cutting along low‑cohesion edges that minimize cross‑group coupling.[^11][^3][^8]

The Plan Grouper output should be an explicit JSON (see schema section) listing groups with `service`, `scope`, `changes`, `tests`, `verification`, `acceptanceCriteria`, and `dependencies`. The difficulty score and token budget estimates help the orchestrator decide whether to run a group in one coder subagent or break it further (e.g., separate backend and frontend sub‑groups when combined scope is too large but still keep them under the same service group for observability).[^3][^10][^8]

Shared context / prompt design
------------------------------

Base Context Compiler should produce two layers:

* **Base head** – a concise, cache‑friendly document summarizing architecture, internal services, code layout, test infrastructure, coding conventions, and project‑specific norms; this is referenced and/or included in the stable shared prefix of every coder subagent.[^5][^4][^6]
* **Service‑local heads** – small per‑service docs (e.g., `docs/services/auth.md`) capturing service boundaries, key endpoints, data models, and typical test patterns; these can be selectively injected into subagents working on that service.[^7][^18][^6]

The prompt scaffolding for each coder subagent then looks like:

1. **System message**: “You are a senior software engineer working on project X. Respect established architecture, tests, and internal service boundaries. You follow TDD/integration‑first patterns and report structured results.” (stable across agents).[^22][^23][^5]
2. **User prefix**: Base context summary, plus link or excerpt of relevant service docs and the spec of this group (fields from the group JSON, including tests and acceptance criteria). Stable except for the small per‑group portion.[^5][^6][^7]
3. **User tail**: The current iteration’s task description, including any updated dependency outputs or surprises reported by upstream groups; this changes with each call but is kept small.[^15][^16][^4]

Following Anthropic’s prompt caching guidance, you mark system and base context as cacheable and avoid editing that portion mid‑run, while only the group tail and iteration state vary.  This gives high cache hit rates across sibling subagents, because they all share the same head and only differ in the small group tail, and prevents overflow by keeping the base doc small and pushing large logs or full file contents into MCP/file tools instead of the core prompt.[^4][^6][^7][^19][^5]

Claude Code execution patterns for this architecture
----------------------------------------------------

For execution, you use Claude Code’s subagents and worktrees as the primary primitives. Each group maps to:

```
* A dedicated git worktree and branch (`git worktree add ../proj-group-<id> -b group/<id>`), giving the coder subagent an isolated file set and commit history.[^17][^18][^1][^19]
```

* A coder subagent definition in `.claude/agents/` that understands the group JSON schema, reads the base context artifact, and runs the gather → implement → verify loop you described (implementation, tests, integration checks, acceptance verification).[^31][^2][^22]

The orchestrator can be either a Claude Code agent (“orchestrator.md”) that executes in the planning session, or an external Python/shell script that uses the `claude` CLI to spawn sessions in each worktree; in both cases, you avoid direct API usage and stay within your subscription.  Dynamic workflows are useful reference patterns here (plan in code, adversarial verification, resumable runs), but given your preference to keep planning and execution separate and avoid mega‑workloads, you can borrow the idea of “plan leaves the context window, becomes executable code” without enabling ultracode everywhere.[^32][^33][^34][^16][^1][^2][^15][^19]

For dependency scheduling, you can emulate Co‑Coder’s dependency‑aware scheduler: independent groups run in parallel; groups with edges wait until upstream groups report completion and verification; if a group reports surprises (missing dependencies, different interfaces, failing tests), orchestrator updates the graph and rewrites dependent groups before launching them.[^9][^10][^3][^8]

Prompt and schema proposals
---------------------------

### Plan Grouper skill/prompt (Claude Code skill)

**Skill description snippet (SKILL.md style):**

> You are Plan Grouper, a planning‑only skill that takes a large project spec and reorganizes it into small, testable execution groups aligned to internal service boundaries. Each group must include implementation tasks, unit/integration tests, verification steps, acceptance criteria, and explicit dependencies on other groups. You optimize for observability and success rate, not raw parallelism, and you avoid both giant groups and excessive micro‑splits.

**Core behavior outline:**

1. Parse the spec into requirements and map each to one or more internal services using architecture docs and base context.
2. Build a task dependency graph (services, APIs, data contracts) and compute difficulty scores for candidate groups.
3. Emit a JSON array of `Group` objects (schema below), ensuring each group is independently testable and has explicit tests/verification and acceptance criteria.
4. Ensure groups that cross services either (a) clearly mark cross‑service dependencies, or (b) are split along service boundaries when difficulty is too high.

### Base Context Compiler skill/prompt

**Skill description snippet:**

> You are Base Context Compiler, a documentation‑oriented skill that reads the current repository, architecture docs, and CLAUDE.md, then produces a compact base context document plus per‑service summaries for use by coder subagents. You optimize for prompt caching: the base document must be small, stable, and cache‑friendly, and per‑service docs must focus on boundaries, common patterns, and test strategies.

**Core behavior outline:**

1. Scan architecture files, service directories, and existing docs to identify internal services, key modules, and test harnesses.
2. Produce `docs/context/base.md` with architecture overview, coding conventions, internal service list, and global constraints (auth, logging, observability).
3. Produce `docs/services/<service>.md` for each service, summarizing endpoints, data models, key flows, and typical tests.
4. Output a small index JSON listing these docs and their intended use in prompts (e.g., `baseHead`, `serviceHead`).

### Execution Orchestrator agent/prompt

**Agent description snippet (or external script design):**

> You are Execution Orchestrator, a minimal scheduler for Claude Code coder subagents. Given a groups JSON and a dependency graph, you schedule independent groups in parallel, wait on dependencies, and update the graph when subagents report surprises. You never write business code yourself; you only launch subagents, track progress, read their structured reports, and rewrite unfinished groups and downstream dependencies when reality changes.

**Core behavior outline:**

1. Read `groups.json` and `dependencies.json`.
2. For each group with `status=ready` and no unmet dependencies, create a git worktree and launch a coder subagent session with the appropriate base context and group spec.
3. When a subagent finishes, read its final report JSON; if `status=success` and verification/tests passed, mark the group complete and unblock dependents; if `status=warning` or `status=failed` with surprises, modify group specs and dependencies and potentially merge or re‑split groups before re‑launching.
4. Keep orchestration logic as data + small control code; avoid embedding large plans inside LLM context.

Sample schemas
--------------

### Group output JSON schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "ExecutionGroup",
  "type": "object",
  "properties": {
    "id": { "type": "string" },
    "service": { "type": "string" },
    "title": { "type": "string" },
    "summary": { "type": "string" },
    "difficultyScore": { "type": "number" },
    "estimatedTokens": { "type": "number" },
    "changes": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "file": { "type": "string" },
          "description": { "type": "string" },
          "kind": { "type": "string" }  // e.g. "backend", "frontend", "infra"
        },
        "required": ["file", "description", "kind"]
      }
    },
    "unitTests": {
      "type": "array",
      "items": { "type": "string" }
    },
    "integrationSteps": {
      "type": "array",
      "items": { "type": "string" }
    },
    "verificationChecklistId": { "type": "string" },
    "acceptanceCriteria": {
      "type": "array",
      "items": { "type": "string" }
    },
    "dependencies": {
      "type": "array",
      "items": { "type": "string" }  // ids of other groups
    },
    "status": {
      "type": "string",
      "enum": ["ready", "running", "blocked", "completed", "failed"]
    }
  },
  "required": ["id", "service", "title", "summary", "difficultyScore",
               "changes", "unitTests", "integrationSteps",
               "acceptanceCriteria", "dependencies", "status"]
}
```


### Dependency graph representation

A simple adjacency list with edge attributes for dependency type and strength:

```json
{
  "nodes": [
    { "id": "G1", "service": "auth" },
    { "id": "G2", "service": "billing" }
  ],
  "edges": [
    {
      "from": "G1",
      "to": "G2",
      "type": "data_contract",
      "strength": 0.8
    }
  ]
}
```

You can compute difficulty and splitting points using strength and node degrees, following cohesion‑aware and runtime‑structured decomposition principles.[^3][^8]

### Per‑group verification checklist schema

```json
{
  "id": "VC-G1",
  "groupId": "G1",
  "items": [
    {
      "id": "VC-G1-UT",
      "description": "All new/updated unit tests pass locally",
      "kind": "unit",
      "required": true
    },
    {
      "id": "VC-G1-IT",
      "description": "Integration test suite for auth service passes",
      "kind": "integration",
      "required": true
    },
    {
      "id": "VC-G1-AC",
      "description": "Acceptance criteria A, B, C verified manually or via tests",
      "kind": "acceptance",
      "required": true
    },
    {
      "id": "VC-G1-OBS",
      "description": "Logs/metrics show expected behavior, no new errors",
      "kind": "observability",
      "required": false
    }
  ]
}
```

This checklist is emitted by Plan Grouper and consumed by the coder subagent’s verify step and the orchestrator’s status logic.[^23][^30][^22]

### Subagent final report format

```json
{
  "groupId": "G1",
  "worktreePath": "../proj-group-G1",
  "branch": "group/G1",
  "status": "success",  // "success" | "warning" | "failed"
  "summary": "Implemented feature X in auth service and added tests.",
  "filesChanged": [
    "auth/feature_x.py",
    "tests/auth/test_feature_x.py"
  ],
  "verification": {
    "checklistId": "VC-G1",
    "items": [
      { "id": "VC-G1-UT", "status": "pass" },
      { "id": "VC-G1-IT", "status": "pass" },
      { "id": "VC-G1-AC", "status": "pass" },
      { "id": "VC-G1-OBS", "status": "pass" }
    ]
  },
  "surprises": [
    {
      "kind": "dependency",
      "description": "Billing service API did not support new field; added stub.",
      "affectedGroups": ["G2"]
    }
  ],
  "logsPath": "logs/groups/G1.log",
  "nextActions": [
    "Update group G2 spec to reflect new billing stub."
  ]
}
```

The orchestrator reads this, updates the dependency graph, and rewrites downstream group specs when `surprises` are present.[^15][^9][^3]

Shortlist of repos / plugins / tools
------------------------------------

* **Compound Engineering plugin** – official Claude Code plugin with Plan → Work → Review → Compound workflows, CLAUDE.md and AGENTS.md scaffolding, and multi‑agent code review patterns.[^24][^25][^23]
* **Superpowers** – core skills library for Claude Code (TDD, debugging, collaboration patterns, best‑practice workflows) designed as an agentic skills framework.[^21][^22]
* **Workflow orchestration plugin (barkain)** – plugin for multi‑step workflow orchestration, automatic task decomposition, parallel agent execution, and plan mode integration; good reference for orchestrator patterns.[^20]
* **wshobson/agents** – large subagent library for Claude Code, useful for coder, reviewer, and tester subagents and for patterns of automatic delegation.[^19]
* **Efficient Claude Code: Context Parallelism \& Sub‑Agents** – practical guide showing worktree‑per‑task, session‑per‑worktree, and subagent specialization; useful concrete patterns for your worktree isolation requirement.[^18][^19]

Shortlist of scientific papers
-------------------------------

* **Cohesion‑Aware Task Partitioning for Multi‑Agent Coding (Co‑Coder)** – dependency‑graph partitioning, cohesion‑aware groups, dependency‑aware scheduler; directly relevant to repository‑level software engineering and multi‑agent coding.[^3]
* **Runtime‑Structured Task Decomposition for Agentic Coding Systems** – structures decomposition around runtime behavior and control/data flows; emphasizes decomposition quality and balanced communication/computation.[^8]
* **A Multi‑Agent Framework for Complex Code Tasks** – describes multi‑agent architectures for complex software tasks, including code migration and refactoring; useful for orchestration primitives.[^13]
* **Utility‑Aware Task Decomposition and Exchange across LLM Agents** – formalizes utility trade‑offs in splitting tasks, focusing on communication/computation balance and success rate.[^11]
* **Modular Task Decomposition and Dynamic Collaboration in Multi‑Agent Systems Driven by LLMs** – hierarchical modular decomposition, dynamic routing, global consistency; matches your desire for adaptive orchestration.[^9]
* **Dynamic Task Decomposition and Agent Generation (TDAG) with ItineraryBench** – dynamic decomposition with automated subagent creation and partial credit evaluation; relevant for adaptation and partial success measurement.[^12]

Orchestrator design: skills vs launcher vs workflows
----------------------------------------------------

Given the docs and your constraints, a hybrid approach is most robust:

* Use a **Claude Code orchestrator agent** for small/medium graphs where the number of groups is modest and scheduling decisions can fit in a parent session; this lets you keep everything inside CLAUDE.md and agents, and reuse existing skills (Compound, Superpowers).[^32][^2][^21][^23]
* Use an **external shell/Python launcher** for large graphs or repeated runs, calling `claude` with different `cwd` (worktrees) and agent files, and tracking group status in local JSON; this keeps orchestration logic in your own code, outside the LLM context, similar to how dynamic workflows move plans into JavaScript scripts.[^16][^1][^15][^19]
* Treat **dynamic workflows** as an opt‑in harness for extraordinary jobs (mass migrations, codebase‑wide audits) where you want adversarial cross‑checking and resumable runs, but avoid using them for everyday planning/execution to keep token usage and complexity under control.[^33][^34][^16][^15]

A purely skills/subagents orchestration is possible but becomes brittle once group counts grow and you need complex dependency updates; a formal workflow framework (dynamic workflows) is powerful but heavy and tends to produce monolithic, opaque runs that conflict with your desire for visible, non‑hype multi‑agent systems.  The hybrid gives you clear control over scheduling and rewriting, plus alignment with Anthropic’s recommended harness patterns, without committing to API‑level orchestration.[^16][^15][^3]

Risks / failure modes / tradeoffs
---------------------------------

**Over‑ and under‑decomposition**: if Plan Grouper over‑splits, you’ll pay in orchestration overhead and context transfer, and coding agents may struggle to maintain coherence across many tiny tasks; if it under‑splits, groups may overflow context or become hard to verify end‑to‑end.  Difficulty‑based splitting mitigates this, but only if your metrics track real change impact and verification cost, not just structural size.[^11][^8][^3]

**Prompt and cache fragility**: relying on cache‑friendly shared prefixes requires discipline: if Base Context Compiler produces oversized or volatile docs, you’ll lose cache hits and reintroduce overflow and latency; frequent edits to cached head segments also break Anthropic’s caching assumptions.  Forked session inheritance is convenient but fragile: when parent contexts drift or bloat, siblings inherit noise and may misinterpret outdated base states.[^2][^6][^7][^4][^5][^19]

**Orchestrator complexity and adaptation**: a too‑clever orchestrator (especially if implemented in LLM prompts) risks becoming a hidden, monolithic agent itself, undermining your goal of a minimal, robust scheduler; meanwhile, insufficient adaptation logic can cause cascaded failures when upstream groups discover surprises that are not propagated properly.  Keeping orchestration logic in a small external script or a narrow Claude agent, with explicit graph rewrites, reduces this risk.[^12][^9][^13]

**Token and compute cost**: parallel subagents, worktrees, and dynamic workflows all multiply token usage; dynamic workflows in particular can consume significantly more tokens than typical sessions.  Your architecture’s emphasis on small, difficulty‑bounded groups, shared cached prefixes, and reusing base context across siblings is an effective mitigation, but you’ll still need monitoring (CLI indicators, cost dashboards) and guardrails on max concurrent groups.[^34][^33][^2][^15]

Overall, grounding your design in cohesion‑aware graph decomposition, cache‑friendly shared context patterns, and Claude Code’s native subagents/worktrees produces the system you described: many small, vertical, service‑aligned execution groups; each in its own worktree; each run by a coder subagent with gather → implement → verify loop; orchestrated by a minimal scheduler that adapts as reality evolves, while keeping context efficient and avoiding monolithic multi‑agent hype.[^1][^6][^21][^23][^5][^10][^3]
<span style="display:none">[^35][^36][^37][^38][^39][^40][^41][^42][^43][^44][^45][^46][^47][^48][^49]</span>

<div align="center">⁂</div>

[^1]: https://code.claude.com/docs/fr/worktrees

[^2]: https://code.claude.com/docs/en/agents

[^3]: https://papers.cool/arxiv/2606.00953

[^4]: https://code.claude.com/docs/id/prompt-caching

[^5]: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching?s=09

[^6]: https://blog.devaubree.fr/en/blog/prompt-caching-claude-code/

[^7]: https://callsphere.ai/blog/prompt-caching-architecture-in-claude-code-explained

[^8]: https://arxiv.org/html/2605.15425

[^9]: https://www.arxiv.org/abs/2511.01149

[^10]: https://www.perplexity.ai/search/d7821e42-a17b-4931-8b09-911355eadb4f

[^11]: https://multiagents.org/2026_papers/utility_aware_task_decomposition.pdf

[^12]: https://www.sciencedirect.com/science/article/abs/pii/S0893608025000796

[^13]: https://arxiv.org/html/2501.06625v1

[^14]: https://code.claude.com/docs/en/workflows

[^15]: https://claude.com/blog/introducing-dynamic-workflows-in-claude-code

[^16]: https://claude.com/blog/a-harness-for-every-task-dynamic-workflows-in-claude-code

[^17]: https://code.claude.com/docs/en/worktrees

[^18]: https://claudeguide.io/claude-code-worktree-parallel

[^19]: https://www.agalanov.com/notes/efficient-claude-code-context-parallelism-sub-agents/

[^20]: https://github.com/barkain/claude-code-workflow-orchestration

[^21]: https://github.com/ivan-magda/claude-superpowers

[^22]: https://github.com/obra/superpowers/blob/main/.claude-plugin/plugin.json

[^23]: https://github.com/EveryInc/compound-engineering-plugin

[^24]: https://github.com/EveryInc/compound-engineering-plugin/blob/main/CLAUDE.md

[^25]: https://www.gitgenius.co/repos/EveryInc/compound-engineering-plugin

[^26]: https://www.perplexity.ai/search/40fd264e-bba2-45b3-ad2b-05e8aacd8fe8

[^27]: https://www.perplexity.ai/search/b1605549-a11b-485e-a682-e84ce57cfc2c

[^28]: https://www.perplexity.ai/search/9db78848-64ee-4a66-8c4a-6ba1976ea402

[^29]: https://www.perplexity.ai/search/d3c1cb0d-d640-4adc-807c-3eb52eec95dd

[^30]: https://www.perplexity.ai/search/4f1a7294-c845-4af0-9ce1-b2425c27224e

[^31]: https://code.claude.com/docs/de/sub-agents

[^32]: https://www.perplexity.ai/search/b1ebe797-ba3d-422e-bfc8-1d9c99be4cee

[^33]: https://dev.to/stacknotice/claude-code-dynamic-workflows-the-complete-practical-guide-2026-3ion

[^34]: https://x.com/akshay_pachaar/status/2060413985925820525

[^35]: https://arxiv.org/html/2510.07772v1

[^36]: https://code.claude.com/docs/id/workflows

[^37]: https://code.claude.com/docs/ja/workflows

[^38]: https://www.youtube.com/watch?v=Aa_6bmzDc80

[^39]: https://claudefa.st/blog/guide/development/dynamic-workflows

[^40]: https://www.youtube.com/watch?v=gV5XCHVWXmo

[^41]: https://code.claude.com/docs/ru/workflows

[^42]: https://www.youtube.com/watch?v=U_bTDSBWRMg

[^43]: https://github.com/everyinc/compound-engineering-plugin

[^44]: https://every.to/guides/compound-engineering

[^45]: https://github.com/EveryInc/compound-engineering-plugin/blob/main/package.json

[^46]: https://aitoolly.com/ai-news/article/2026-02-11-everyinc-releases-official-claude-code-compound-engineering-plugin-on-github-trending

[^47]: https://www.youtube.com/watch?v=x8SvOSiYJrQ

[^48]: https://github.com/EveryInc/every-marketplace

[^49]: https://www.instagram.com/p/DZDv6IuoEpT/

