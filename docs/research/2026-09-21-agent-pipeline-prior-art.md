# Prior art: typed agent pipelines, node registries, and dynamic graphs

**Date:** 2026-09-21
**Purpose:** the durable record of the prior-art research behind
`docs/brainstorms/2026-09-21-unit-recipes-requirements.md`. Written so nobody
has to run this research again.
**Companion:** `docs/research/2026-09-21-kpi-optimization-prior-art.md` covers
the construction-versus-optimization loop (FunSearch → AlphaEvolve →
ShinkaEvolve, Karpathy's autoresearch, reward hacking, small-budget
statistics). That is the input to the *next* brainstorm; this document is the
input to the one we just finished.

______________________________________________________________________

## 0. How to read this — provenance matters

This research ran under an awkward constraint worth recording, because it
shaped what we can and cannot trust.

**`api.perplexity.ai` is blocked by this environment's network policy.** The
agent proxy answers `403` to `CONNECT` for that host. An API key was supplied
and written to a gitignored `.envrc`, and it made no difference — the request
never left the container. The built-in `WebSearch`/`WebFetch` tools route
differently and work fine. Several documentation domains are *also* blocked for
direct fetch (`arxiv.org`, `docs.langchain.com`, `airflow.apache.org`,
`docs.dagster.io`, `docs.ray.io`, among others), so some claims below rest on
search-engine-surfaced quotations rather than first-party fetches.

Consequently the underlying material came from three channels of different
quality:

| Channel                                                          | Trust                                                         | What it produced                                                                            |
| ---------------------------------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Perplexity Deep Research, run by the human outside the container | **High** — sourced, URL-cited                                 | The registry survey, context passing, dynamic pipelines, cost gates                         |
| `WebSearch`/`WebFetch` agents inside the container               | **High** — sourced, but some domains only via search snippets | Verification of version-sensitive claims, the AOrchestra find, the autoresearch source read |
| Model training knowledge (cutoff May 2026), zero retrieval       | **Low — superseded**                                          | Two early reports, kept only for provenance                                                 |

Where the sourced and unsourced material disagree, **the sourced material
wins.** The two training-knowledge reports were retained in the working
directory but are not reflected here except where later verified.

**One correction worth carrying forward**, because it was repeated in early
drafts: Drew Breunig's post is titled **"How Long Contexts Fail"**, not
"context rot" — a related but distinct term from elsewhere in the discourse.
Two of its four modes are also commonly misquoted; the accurate definitions are
in §6.

______________________________________________________________________

## 1. The headline finding

Two independent investigations — Perplexity Deep Research run by the human, and
a WebSearch agent run in-session — were given the same question and assessed
**largely disjoint sets of systems**. They reached the same verdict:

> **No published system — paper, open-source project, or commercial product —
> offers a pluggable unit-type registry that simultaneously owns the prompt
> template, tool allowlist, completion contract, review mode, merge behaviour
> and budget model, and uses it to route units of a persisted plan.**

Perplexity assessed GraphBit, LangGraph, YYLO, SWE-AF, AgentEval, Gradientsys,
ECOA and the DataFlow-Agent family. The agent assessed Block Goose, LangChain
`deepagents`, Roo Code, Claude Code subagents, OpenHands, GitHub Agentic
Workflows, Dify and AOrchestra. Almost no overlap; same conclusion. That
convergence is what makes it credible — a single source saying "nobody has
built this" is a failure of search, two disjoint surveys saying it is evidence.

Perplexity's phrasing is worth quoting because it calibrates the claim
correctly:

> "Your proposed `kind` field plus per-kind bundle appears to be **novel in its
> completeness**, even though it is inspired by ideas that are individually
> present elsewhere. … It is more like **the next logical step that systems
> like GraphBit, LangGraph, YYLO, and deep research pipelines are all trending
> toward but have not yet fully articulated** as a generalized, pluggable
> abstraction."

### The three gaps, found by both surveys

Everything else in the Unit Recipe design exists somewhere. These three were
found **nowhere**:

1. **Per-recipe merge behaviour.** No system lets a node type decide whether
   its output is a git merge, an artifact registration, or nothing. Claude
   Code's `isolation: worktree` is the nearest relative and carries no merge
   policy at all.
2. **A machine-readable cost estimate gating an LLM step.** Systems have
   turn caps (Goose `max_turns`, Claude Code `maxTurns`), wall-clock caps
   (gh-aw `timeout_minutes`, Argo `activeDeadlineSeconds`) and post-hoc spend
   limits. Nobody couples a *pre-flight machine-readable estimate* to an
   approval policy for an LLM step. The Terraform → Infracost → Sentinel shape
   is mature in infrastructure and simply un-ported to the agent world (§7).
3. **Review mode as a per-type field.** `deepagents`' `interrupt_on` is
   human-interrupt only; Goose's `retry.checks` is validation only. Nothing
   expresses "for this type, review means an LLM reviewer; for that type, a
   schema validator; for this other one, a threshold."

**Design consequence:** we took the industry shape and closed its three gaps.
That is the defensible story, and it is also a warning — three gaps means three
things nobody has debugged before us.

______________________________________________________________________

## 2. AOrchestra — the counter-thesis, and our answer to it

**AOrchestra: Automating Sub-Agent Creation for Agentic Orchestration.**
ICML 2026. arXiv **2602.03786**. <https://arxiv.org/abs/2602.03786> ·
<https://icml.cc/virtual/2026/poster/65930>

This is the single most important paper for our design, because it argues
against a static registry.

**What it does.** It models any agent as the four-tuple **(Model, Task, Tools,
Context)** and gives the orchestrator exactly two actions: `Delegate` — fill in
the four-tuple and assign a subtask — and `Finish`. Sub-agents are
**dynamically synthesized per task**, not selected from a preconfigured roster.
The framing is explicitly against manually preconfigured rosters.

**Its capability-allocation argument** is the part that most resembles our tool
allowlist, and it is worth quoting in substance: a sub-agent that only needs to
inspect files does not necessarily receive code-editing tools, while one that
needs to verify a result is given access to the relevant evaluator. That is the
same instinct as a per-recipe tool allowlist — arrived at from the opposite
direction.

**Its result.** 16.28% relative improvement over the strongest baseline on
GAIA, SWE-Bench and Terminal-Bench with Gemini-3-Flash, and it frames model
choice as a controllable performance–cost Pareto trade-off.

**Why this is a genuine challenge to us.** If dynamically synthesized agents
beat hand-built rosters by 16% on three benchmarks, a fixed registry of three
recipes looks like the losing side of a published result.

### Our answer

**The recipe defines the envelope and the policy; the unit fills in the
four-tuple within it.**

Concretely: a recipe declares a *maximum* tool set, an *allowed* model set, a
budget *policy*, a completion contract and a merge behaviour. The plan — written
by a human, per our standing principle — picks the specific tools, model and
context for each unit inside that envelope. This concedes what AOrchestra
actually demonstrates (per-task capability allocation beats one-size-fits-all)
while keeping what it gives up (auditability, per-type budgeting, reproducible
replay, and a human owning topology).

It is also worth being precise about what AOrchestra's result does *not* show.
It measures task success on benchmarks where the orchestrator is free to spend;
it does not measure reproducibility, cost predictability, or what happens on
retry when the synthesis step is nondeterministic. Those are exactly the
properties a long autonomous run in someone's repository needs, and they are
the properties §5's failure list says dynamic synthesis erodes.

**Watch-out:** if we ever find ourselves wanting the plan to be *generated*
rather than written, AOrchestra is the paper to re-read first, and this
reconciliation is the thing to re-examine.

______________________________________________________________________

## 3. The registry survey — what every close system actually carries

The pattern across production systems is unambiguous and it settled our
`kind`-on-unit decision:

> **A node's type is a first-class declarative field, and a registry maps
> type → executor behind a uniform interface.**

The *workflow engines* do this. The *agent frameworks* mostly do not — they use
"just a different callable" plus prebuilt constructors — **and that is precisely
why they struggle with heterogeneous budgeting and review.** Making the type
first-class puts us on the workflow-engine side of that line.

### Tier A — agent systems with a registry of named executor types

**Block Goose recipes + subrecipes — closest overall.**
<https://goose-docs.ai/docs/guides/recipes/recipe-reference/> ·
<https://block.github.io/goose/docs/guides/recipes/subrecipes/>

A recipe is a YAML file bundling `version`, `title`, `description`,
`instructions` + `prompt` (Jinja2-templated), `parameters`, `extensions` (the
MCP/builtin tool set), `settings` (provider, model, temperature, **`max_turns`**),
`activities`, **`response`** (a JSON schema the final output is validated
against), **`retry`** (ordered success checks; on failure with attempts
remaining the agent resets and restarts), and `sub_recipes`.

That is prompt + tools + model + completion contract + verification loop + turn
budget in one registerable, shareable, parameterized unit. Missing versus us:
merge behaviour, a cost estimate, review mode beyond schema-plus-checks, a
sizing model.

**This is where our word `recipe` comes from**, and the `response` + `retry`
pairing is the shape we adopted for per-recipe verification.

**LangChain `deepagents` `SubAgent` — closest code analogue.**
<https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py>

`SubAgent` TypedDict fields, read from source: `name` (required),
`description` (required), `system_prompt`, `mode: Literal["isolated","fork"]`,
`tools`, `model`, `middleware`, `interrupt_on`, `skills`, `permissions`,
`response_format`. Registration compiles them into `subagent_graphs: dict[name -> runnable]`, exposed as one `task()` tool dispatching on `subagent_type`.

**That is exactly the `type → executor` registry shape, one level up (agent,
not plan unit).** We adopted this field list as the starting schema and added
the three missing fields.

One field from it we would not otherwise have listed: **`mode: isolated | fork`** — does the unit start from a clean context or inherit the parent's?
Every close system has a version of it (Claude Code's `omitClaudeMd`, OpenAI's
`handoff_input_filter`). It is a per-type decision, and the context-failure
literature (§6) argues for defaulting to `isolated`.

**Roo Code custom modes + Orchestrator (Boomerang) mode.**
<https://docs.roocode.com/features/custom-modes>

A user-extensible mode registry: `slug`, `roleDefinition` (placed at the start
of the system prompt), `groups` (tool allowlist, where `edit` can carry a
`fileRegex` file restriction), `customInstructions`, and since v3.12 a per-mode
model profile. The Orchestrator mode delegates subtasks and has the mode
registry injected into its system prompt so it knows what it can delegate to.
Missing: contract, budget, merge, review mode.

**Claude Code subagents.** <https://code.claude.com/docs/en/sub-agents>

Markdown + YAML frontmatter registry with a documented resolution order.
Fields include `name`, `description`, `tools`, `disallowedTools`, `model`,
`permissionMode`, `maxTurns`, `skills`, `mcpServers`, `hooks`, `memory`,
`background`, `omitClaudeMd`, `effort`, **`isolation: worktree`**.

The docs are explicit that **there is no completion contract or structured
output schema per subagent** — output is a natural-language summary, marked
partial at `maxTurns`. Note that `isolation: worktree` already exists with *no
merge policy attached*, which is the closest anything came to our gap #1.

**OpenHands agent registry.**
<https://github.com/OpenHands/openhands> · ICLR 2025:
<https://openreview.net/pdf/95990590797cff8b93c33af989ecf4ac58bde9bb>

A class-level registry: `Agent.register('CodeActAgent', CodeActAgent)`,
`Agent.register('BrowsingAgent', BrowsingAgent)`, selected via config. Each
registered class supplies its own system prompt and action/tool space. **This is
the cleanest published answer to "how does a third party add a type" in the
agent world**, and it is the shape our registry should resemble.

**GitHub Agentic Workflows (gh-aw).** <https://github.com/github/gh-aw>

Markdown body = prompt; frontmatter = triggers, `permissions`, `tools`,
`network`, `engine`, `timeout_minutes`, and **`safe-outputs`** — declared
pre-approved side effects (create-issue, add-comment, create-pull-request) that
"buffer configured writes, validate them, and apply them in separate jobs with
scoped permissions."

**This is the best published model for per-recipe merge behaviour**, and it is
what R15 is built on: the type declares which side effects it may produce, and
a separate privileged step validates and applies them. It is simultaneously a
merge policy and a blast-radius control.

**BMAD-METHOD v6.** <https://github.com/bmad-code-org/BMAD-METHOD> — agents as
Markdown+YAML persona files, YAML workflows sequencing typed roles.
Registry-shaped but types *roles*, not units; no contract, budget or merge.

### Tier B — typed node registries in workflow engines

**Dify — a literal pluggable node-kind registry in Python.**
<https://github.com/langgenius/dify/blob/main/api/core/workflow/nodes/base/node.py>

`NodeType` enum; `BaseNode[TNodeData]` with `_node_type` / `_node_data_cls`
class vars per subclass (`ToolNode(BaseNode[ToolNodeData])`, `_node_type = NodeType.TOOL`). Kinds: LLM, Tool, Code, Knowledge Retrieval, Question
Classifier, HTTP Request, Agent. **This is the Python shape to copy.**

**Argo Workflows — the best structural analogue for a unit type.**
<https://argo-workflows.readthedocs.io/en/latest/workflow-concepts/>

A template is a tagged union, and the controller switches on which field is
present, taking entirely different execution paths: `container`, `containerSet`,
`script`, `resource`, `suspend` (runs no pod at all), `http` (runs in the
controller), `data`, `plugin`.

**A refinement that changed our design:** Argo splits **template definitions**
from **template invokers** — `steps` and `dag` "don't execute work themselves
but define how other templates are invoked and composed." The lesson:
**composition is a separate axis from type.** Do not let `group`, `dag` or
`fanout` become recipes.

Its **`plugin` template** is the published answer to third-party extension:
"Plugin templates let Argo Workflows use executor plugins, extending behavior
without modifying Argo core", backed by an `ExecutorPlugin` CR declaring a
sidecar HTTP server. Disabled by default (`ARGO_EXECUTOR_PLUGINS=true`).
<https://argo-workflows.readthedocs.io/en/latest/executor_plugins/>

**Flyte — `task_type` plus a backend plugin registry.**
<https://docs.flyte.org/en/latest/user_guide/extending/backend_plugins.html>

"The `TaskTemplate` consists of the interface, **`task_type` identifier**, some
metadata and other fields. An important field to note is **`custom`, which is
essentially an unstructured JSON**" — how a plugin carries configuration the
core engine does not understand.

**We stole this directly:** R2 gives the registry a `custom` mapping the core
never interprets, which is what makes a third-party recipe possible without a
core change.

Flyte also has **first-class gate nodes** — `flytekit.sleep`,
`flytekit.wait_for_input(name, expected_type, timeout)`, `flytekit.approve` —
where the human's answer is **a typed value that is a real workflow output**,
consumable downstream and branchable. That is the cleanest "interview then
branch" primitive in any production engine, and it is the model for a future
`interview` recipe.

*Caveat:* canonical Flyte docs now live under union.ai and the v2 SDK uses
`import flyte as fl`. **flytekit v2 naming is in flux — pin a version before
coding against it.**

**Temporal — the sharpest split in the field.**
<https://docs.temporal.io/child-workflows>

**Workflow** (deterministic, replayable, no I/O) versus **Activity**
(side effects, own timeouts, retry policy, heartbeats). If you want one mental
model for "each type gets its own executor, budget and failure semantics", the
Activity interface is the thing to copy: `StartToCloseTimeout`,
`ScheduleToCloseTimeout`, `HeartbeatTimeout`,
`RetryPolicy.non_retryable_error_types`.

**Its replay discipline is the rule we should internalise even though we are
not adopting dynamic graphs:** workflow code replays from event history, so any
decision must be deterministic relative to recorded history. **Put LLM calls in
Activities, never in workflow code** — record the result, replay from the
record.

**Others, briefly.** Airflow: type = Operator subclass, with placement routing
kept separate (`queue`, `pool`, `executor_config`) — and Airflow/Prefect
historically *conflated* semantics with placement and spent years unwinding it.
Metaflow keeps them clean (`@step` for the node, `@kubernetes`/`@resources` for
placement) and is the model. Dagster: `@asset` vs `@op` vs **`@asset_check`** —
a literal test/evaluate node type. LangGraph: `add_node(name, fn)`, **no type
field**; differentiation by convention plus prebuilts. Haystack 2.x:
`@component` with declared output types and **connect-time type validation** —
good precedent for validating a unit graph *before* running it.

### The lesson we took on our own seam

The warning "don't conflate type with executor" applies to a *different axis*
than ours. Airflow's "executor" means *where a task runs*; our
`Executor = Callable[[GroupContext], Awaitable[GroupState]]` means "run one
group to a terminal state", which is semantic. Dispatching on recipe there is
correct. The transferable caution is to keep any future concurrency or
placement knobs on a separate axis rather than folding them into the recipe.

______________________________________________________________________

## 4. Passing data between heterogeneous steps

The governing sentence, from the sourced research:

> **Messages coordinate; artifacts carry truth; prompts contain only the
> current working set.**

Production systems do **not** pass one agent's transcript to the next. They
treat LLM context as a scarce working set and move durable outputs through
typed, versioned external artifacts. Anthropic's own guidance is to minimize
high-signal context, use lightweight references for just-in-time loading, and
isolate detailed exploration in subagents that return condensed output — often
1,000–2,000 tokens — to the lead.
<https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents>

### The three-layer model

| Layer              | Purpose                                           | Size                 | Ours                                                              |
| ------------------ | ------------------------------------------------- | -------------------- | ----------------------------------------------------------------- |
| **Raw artifact**   | full fidelity, immutable, auditable               | unlimited            | the committed document / `data_dirs` file                         |
| **Manifest entry** | routing, lineage, schema, hash, location, quality | hundreds of tokens   | **the Artifact Manifest (R16)** — this layer did not exist before |
| **Handoff packet** | what the next agent must know to act              | **500–2,000 tokens** | the per-recipe handoff (R10)                                      |

A downstream LLM should see **the handoff packet plus manifest entries**, never
the full upstream payload, with tools to pull detail on demand
(`artifact.describe`, `artifact.search`, `artifact.read(section)`).

**There is no credible universal empirical result that summary, pointer, or
tool-retrieval categorically wins.** The strongest public guidance is a hybrid
just-in-time architecture. The failure shapes are: a plain summary is **lossy
compression** (hides edge cases, citations, uncertainty); pointer-only creates a
**discovery problem** (the agent does not know what to inspect); tool-pull gives
fidelity but costs latency and depends on good search.

### The decision record — what a `synthesize` unit should emit

The sourced research proposed this shape, and R6 is built on it:

```json
{
  "schema_version": "decision.v1",
  "decision_id": "sha256:...",
  "objective": "...",
  "decision": "Use approach B",
  "rationale": [{"claim": "...", "evidence": ["artifact:research-b#finding-4"]}],
  "constraints": ["Must preserve API compatibility"],
  "alternatives_rejected": [{"option": "A", "reason": "...", "evidence": [...]}],
  "implementation_contract": {"files_or_modules": [], "interfaces": [], "acceptance_tests": []},
  "open_questions": [],
  "artifact_refs": ["artifact:research-a", "artifact:research-b"]
}
```

With the rule that every summary claim carries
`claim_id → evidence artifact + exact span → confidence → freshness → contradiction status`. That turns a summary from an ungrounded memo into an
inspectable index.

### Watch-outs

- **Do not use an unbounded `messages: list[...]` reducer as a long-term
  database.** That recreates transcript forwarding under another name.
- **Argo's #1 scaling bug** is confusing `parameters` (small, inline, subject to
  etcd size limits) with `artifacts` (blob store). Keep the two channels
  strictly separate.
- **"Content-addressed" needs qualification.** Dagster assets, Metaflow
  artifacts and Flyte artifacts give persisted, versioned workflow artifacts and
  lineage, but are *not* automatically keyed by payload hash. Compute your own
  `sha256` — which is why R16 requires one.
- **Metaflow explicitly warns against overwriting the same object key**, because
  readers can observe inconsistent versions.
- **OpenAI Agents SDK's default is the trap:** a handoff passes the *entire*
  previous conversation unless you supply an `input_filter`. And filtering
  `input_items` does **not** redact content already copied into summaries. Its
  `RunConfig.handoff_input_filter` gives a *default per-edge policy*, which is
  the "the type supplies the handoff policy, not the plan author" idea we
  adopted in R10.

______________________________________________________________________

## 5. Dynamic pipelines — and why we chose not to have them

This section exists to record a decision that is now a **standing design
principle**: the pipeline is human-planned, agents never create nodes, and a
node that cannot finish escalates to a human rather than growing the graph.

### The distinction that matters

> Does the system create **instances of known nodes**, or does runtime output
> create an **arbitrary new subgraph**? The former is substantially safer, more
> observable and easier to replay; the latter is more expressive but needs an
> admission-control layer you build yourself.

Almost every production system does the first:

| System                       | Mechanism                               | Freedom  | What it creates                                              |
| ---------------------------- | --------------------------------------- | -------- | ------------------------------------------------------------ |
| Airflow dynamic task mapping | upstream returns list/dict, `.expand()` | low–med  | N instances of a **predeclared** task                        |
| Argo `withParam`             | step emits JSON array                   | med      | N invocations of an existing template                        |
| Argo recursive templates     | template calls child recursively        | med–high | nested calls; needs explicit termination                     |
| Flyte `@dynamic`             | runtime Python compiles a sub-workflow  | med–high | subworkflow of **known typed entities**                      |
| Flyte `map_task`             | map a known task                        | low–med  | N executions, with `min_success_ratio`                       |
| **Temporal child workflows** | parent starts children at runtime       | **high** | independent durable executions — *you own admission control* |
| **LangGraph `Send`**         | routing fn returns `Send(node, state)`  | med      | dynamic invocations of **predeclared** nodes                 |
| Dagster `DynamicOut`         | `DynamicOutput(value, mapping_key)`     | low–med  | N mapped ops over stable keys                                |
| Metaflow `foreach`           | branch over runtime list                | low–med  | N branches of predeclared steps                              |
| **GitLab child pipelines**   | job emits CI YAML as an artifact        | **high** | a new pipeline — *executable infrastructure config*          |
| GitHub Actions matrix        | `fromJSON()` in `strategy.matrix`       | low–med  | N copies of one predeclared job (max **256**)                |

The sourced guidance was blunt:

> "Dynamic pipelines are best treated as **runtime expansion of a constrained
> template**, not as an LLM freely editing the live DAG. … **Avoid an arbitrary
> 'agent emits DAG JSON; executor runs it' interface until you have a real
> compiler, static policy checker, and approval model.**"

### The seven documented failure modes of LLM-generated plans

Recorded in full, because these are the reasons behind our principle:

1. **Unbounded decomposition.** The LLM treats every uncertainty as a reason to
   create another task; parallelism hides the damage until throttling or a
   surprise bill.
2. **Syntactically valid, operationally nonsense.** JSON Schema proves
   `depends_on` is an array; it does not prove the task is useful,
   non-duplicative, secure or feasible.
3. **Nondeterministic retry changes history.** The planner emits a different
   work list on retry; runs become irreproducible and partial outputs no longer
   align with the new graph.
4. **Infinite or low-value re-planning loops.** LangGraph's default
   25-superstep `recursion_limit` exists precisely to bound this.
5. **Shared-state races.** Parallel workers touch the same file, ticket or
   reducer.
6. **Privilege escalation through generated workflow definitions.** An LLM
   emitting raw CI YAML may choose images, runners, secrets, deployment jobs.
7. **Estimator gaming or error.** Planner cost predictions are wildly wrong
   when a worker explores or retries tools.

### The caps, and why they do not help us

> **"Platform limits exist to prevent scheduler/database overload, controller
> work amplification, workflow-history exhaustion, and infinite recursion — not
> primarily to save your money. Your system must separately protect the
> economic boundary."**

Documented defaults, for reference: Airflow `core.max_map_length` = **1,024**
(exceeding it *fails* the producing task); Argo controller max template
recursion depth = **100**, plus v4 validation caps of 200 templates/workflow and
200 DAG tasks/template; Temporal history capped at **51,200 events or 50 MB**
with warnings at 10,240/10 MB, at most **2,000 incomplete children** and a
recommendation to stay near 1,000; LangGraph `recursion_limit` = **25
supersteps**; GitHub Actions **256 jobs** per matrix run.

**Four separate quantities need capping** — N, concurrency, depth/replans, and
cumulative cost — and "max fan-out" alone is insufficient. Concurrency controls
burst spend and rate limits but does **not** bound eventual cost.

### If this is ever revisited

The architecture to build, recorded so it does not have to be re-derived:

```
research
  → candidate_manifest
  → schema_validate (extra="forbid")
  → policy_validate (server-side template allow-list)
  → dedupe_and_cycle_check
  → estimate_and_reserve_budget (server-side re-estimate; never trust the planner's number)
  → approved_manifest_vN (immutable)
  → bounded_map(worker)
  → verify_and_collect (serialized)
  → optional_replan_remaining_only
```

With these properties: **the planner is untrusted with respect to topology,
money and tool permissions**; the admission controller, not the planner, decides
whether N becomes runnable work; workers spawn only from server-approved
templates; expansion is **one level by default**; the approved manifest is
immutable and a revision creates `manifest_v2` with an explicit budget delta;
the final verify/merge stage is **serialized**.

And two rules that would apply regardless:

- **Identity:**
  `work_id = H(objective ‖ evidence ‖ template_version ‖ normalized_inputs)`,
  used as the child ID, dedup key, budget reservation key, artifact namespace,
  **worktree namespace**, retry idempotency key and audit correlation. The
  replay key must be content-derived, **not array position** — a reordered LLM
  list must not turn "finding 3" into a different job.
- **Vocabulary:** *"Retry means retry the same approved work. Replan means
  create a new manifest version with an explicit supersession relation. Never
  silently replace a plan during retry."*

**In LLM plans, cycles are semantic, not syntactic.** Graph cycle detection will
not catch "research topic X" spawning "research topic X, phrased differently".
The practical detector is canonicalized work keys plus near-duplicate rejection.

### Why our model is better for what we actually want

Our model — human plans the pipeline; each node repeats until completion or a
cap; the cap hands control to a human — makes every one of the seven failure
modes **moot rather than managed**. There is no decomposition to bound, no
nondeterministic plan to replay, no privilege escalation surface, and no
estimator to game. We already have the escalation machinery that the dynamic
design would have to invent: rounds bounded by a budget, rewrites bounded by
`max_rewrites`, generations bounded by a cap, and a Work Failure that is
terminal by design so a human looks at it.

______________________________________________________________________

## 6. Failure taxonomies worth knowing by name

### MAST — the empirical study

**"Why Do Multi-Agent LLM Systems Fail?"** Cemri, Pan, Yang, Agrawal, Chopra,
Tiwari, Keutzer, Parameswaran, Klein, Ramchandran, Zaharia, Gonzalez, Stoica
(UC Berkeley). arXiv **2503.13657**. **NeurIPS 2025, Datasets & Benchmarks
track.** <https://arxiv.org/abs/2503.13657>

1,600+ annotated traces across 7 multi-agent frameworks; **14 failure modes in
3 categories**; human taxonomy validated at **κ = 0.88**. Ships MAST-Data.

**Failure rates of 41%–86.7% across the evaluated open-source systems —
multi-agent decomposition is not automatically better than a well-built single
agent.**

The three categories: **system/specification design** (disobey task spec,
disobey role spec, step repetition, loss of conversation history, unaware of
termination conditions); **inter-agent misalignment** (conversation reset,
failure to ask for clarification, task derailment, information withholding,
ignored other agent's input, reasoning–action mismatch); **task verification**
(premature termination, no or incomplete verification, incorrect verification).

Frequencies worth remembering: step repetition **15.7%**, reasoning–action
mismatch **13.2%**, unaware of termination **12.4%**, task-spec disobedience
**11.8%**, incorrect verification **9.1%**. An objective-verification
intervention improved ChatDev success by **15.6%**; a role/workflow adjustment
by **9.4%** — orchestration and verification design matter, not just the model.

**Why this drove R13.** The task-verification bucket is the one that hits a
typed-recipe design: heterogeneous types with weak completion contracts fail
**silently**. That is why the completion contract is a schema validated
mechanically on every unit regardless of review intensity.

### "How Long Contexts Fail" — the four modes, stated correctly

Drew Breunig, 2025-06-22.
<https://www.dbreunig.com/2025/06/22/how-contexts-fail-and-how-to-fix-them.html>

- **Poisoning** — a hallucination or stale state enters memory and is reused as
  fact; the error compounds.
- **Distraction** — the model **over-focuses on accumulated history and repeats
  past actions instead of synthesizing novel plans**. Observed past ~100k
  tokens. (Not merely "bulk crowds out instructions.")
- **Confusion** — irrelevant documents or tool definitions dilute the signal and
  lead to wrong tool calls.
- **Clash** — **conflicting information or instructions inside the context**,
  with third-party tool descriptions clashing with your prompt as the named
  example. (Not merely "two steps contribute contradictory facts.")

The corrected definitions are *more* relevant to us: **distraction argues for
bounded per-edge handoffs between recipes; clash argues for per-recipe tool
allowlists.** Supporting evidence cited in the post: Gemini's long-running
Pokémon agent repeating historical actions past ~100k tokens, and a multi-turn
"sharded prompt" experiment showing an average **39% drop**, with o3 falling
**98.1 → 64.1**, when identical information was staged across turns.

### Cognition, "Don't Build Multi-Agents"

Walden Yan. <https://cognition.com/blog/dont-build-multi-agents>

> "even if you give each sub-agent the full initial task description, they
> won't have each other's ongoing intermediate decisions or assumptions."

Read as an argument **for** strong typed handoffs, not against a typed design.

### Context rot

Long pipelines degrade **before** the hard token limit: recall and long-range
reasoning precision fall as context grows — a **performance gradient, not a
hard cliff** — attributed to finite attention capacity, the n² attention
structure, and training distributions weighted to shorter sequences.

The 2025–2026 practical consensus is **not** "multi-agent by default": start
single-agent with tools, structured memory, checkpoints and compaction when work
is sequential and tightly coupled; add **ephemeral parallel subagents** for
independent read-heavy exploration with a clear merge contract; **keep writes
and authoritative decisions centralized.**

______________________________________________________________________

## 7. Cost gates — the mature pattern nobody has ported to agents

Recorded because it is gap #2, and because it is what a future `run` or
`llm-batch` recipe will need.

**The canonical shape is Terraform plan → cost estimate → approval → apply:**
Infracost runs on the plan JSON and posts an estimated delta, CI blocking the
apply; HCP Terraform has built-in cost estimation plus **Sentinel** policies
reading `tfrun.cost_estimate.delta_monthly_cost`; Spacelift and Atlantis do the
same via OPA/Rego or a manual confirmation.

> **The key transferable idea: the estimate is a machine-readable artifact
> produced by a dry-run phase of the same step, and the gate policy is code
> that reads it — not a human eyeballing prose.**

Other instances: **BigQuery `maximumBytesBilled`** plus a dry-run byte estimate,
where the cap **hard-fails the query** rather than warning — the best example of
an estimate-derived hard cap, and notable because the estimate and the cap use
the *same unit*. Snowflake resource monitors (suspend at a % of credit quota).
AWS Budgets Actions (threshold → IAM/SCP blocking launches). **GitHub Actions
`environment` protection rules** — a job does not *start* until required
reviewers approve, i.e. approval before launch.

Two things people get wrong, both of which we would inherit:

1. **The estimate must be a typed artifact** so the gate is a policy function
   and can auto-approve under a threshold. A pure-human gate on every costly
   unit will strangle throughput.
2. **Persist estimate versus actual** so the sizing model is recalibratable —
   which is why R12 records a defaulted price in the grouping trace.

Already in our stack: Claude Code `PreToolUse` hooks can return `ask`/`deny`, so
a gate primitive exists locally.

______________________________________________________________________

## 8. Per-project learnings and watch-outs

| Project                           | Take                                                                                                                                                               | Watch out                                                                                                                  |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| **Block Goose**                   | `response` schema + ordered `retry` checks as the verification shape; the word "recipe"                                                                            | Its recipes have no merge or budget concept                                                                                |
| **LangChain deepagents**          | The `SubAgent` field list as our starting schema; `mode: isolated \| fork`                                                                                         | Types the agent, not the plan unit; no verify step                                                                         |
| **Argo Workflows**                | Tagged-union template types; `plugin` as the extension point; **composition is a separate axis from type**                                                         | Plugins disabled by default; `parameters` vs `artifacts` is the #1 scaling bug                                             |
| **Flyte**                         | `task_type` + registry; **`custom: dict` escape hatch**; typed gate nodes (`wait_for_input`)                                                                       | **v2 SDK naming is in flux — pin a version**; docs moved to union.ai                                                       |
| **Temporal**                      | Activity interface as the per-type contract model; **LLM calls belong in Activities, never workflow code**                                                         | Children do not carry over on Continue-As-New; history caps are real                                                       |
| **OpenHands**                     | `Agent.register(name, cls)` as the cleanest third-party extension shape                                                                                            | Organized around agents and tools, not plan units                                                                          |
| **gh-aw**                         | **`safe-outputs`** as the per-type merge model: declare side effects, apply them in a separate privileged step                                                     | GitHub-specific; the validation step is where all the work is                                                              |
| **Dify**                          | `NodeType` enum + `BaseNode[TNodeData]` — the Python registry shape to copy                                                                                        | Fixed palette, not third-party extensible                                                                                  |
| **Airflow**                       | Operator-subclass typing; **HITL operators exist in 3.1+** (`ApprovalOperator`, `HITLEntryOperator`, `HITLBranchOperator`, in `apache-airflow-providers-standard`) | Historically conflated semantics with placement and spent years unwinding it                                               |
| **Metaflow**                      | The clean model: `@step` for the node, `@kubernetes`/`@resources` for placement                                                                                    | Artifact unpickling breaks on library-version skew between steps                                                           |
| **Dagster**                       | `@asset_check` as a literal test/evaluate node type                                                                                                                | `kinds` supersedes `compute_kind` on assets (1.9+) — **but `@asset_check` still documents `compute_kind`**                 |
| **LangGraph**                     | `Send` for map-reduce; `interrupt()`/`Command(resume=)` for HITL                                                                                                   | **Resume restarts the ENTIRE node, not the interrupt line**; requires a checkpointer; multiple interrupts matched by order |
| **Prefect**                       | `wait_for_input` renders a **UI form from a Pydantic schema** — the best HITL UX precedent                                                                         | `pause` keeps infra alive; `suspend` tears it down                                                                         |
| **Haystack 2.x**                  | **Connect-time type validation** — validate the graph before running it                                                                                            | —                                                                                                                          |
| **Ray**                           | `ray.data.llm` (`build_llm_processor`, `vLLMEngineProcessorConfig`) is a live `llm-batch` backend                                                                  | **Ray Workflows is DEPRECATED — do not cite its dynamic continuations**                                                    |
| **Anthropic / OpenAI Batch APIs** | The `custom_id` + per-item status + retry-only-failed shape; ~50% discount                                                                                         | **Results come back UNORDERED — key by `custom_id`, never by position**                                                    |
| **AOrchestra**                    | Per-task capability allocation beats one-size-fits-all; the (Model, Task, Tools, Context) four-tuple                                                                  | It argues *against* static registries — our envelope/fill-in answer is in §2                                               |
| **GitLab child pipelines**        | The "emit a config artifact, run it as a child" shape, if we ever do dynamic                                                                                       | Generated YAML is executable infrastructure config — a privilege boundary                                                  |

### Batch API specifics, verified

**Anthropic Message Batches:** ≤100,000 requests or 256 MB per batch; most
complete within 1 hour, max 24 hours (unfinished return `expired`); results
retrievable 29 days; **50% discount**; per-item `result.type` of
`succeeded`/`errored`/`canceled`/`expired`. <https://claude.com/blog/message-batches-api>
**OpenAI Batch:** 50,000 requests or 200 MB per job; 24-hour window; 50%
discount; separate `error_file_id`.
*Not confirmed:* the per-model enqueued-token rate limit.

______________________________________________________________________

## 9. What we decided, and what is still unknown

### Decided (see the requirements doc for the full list)

The term is **Unit Recipe** (`kind` is taken by Preflight Kind, `machine` by
machine suspend/wake, `role` by `SessionRole`, `executor` by the scheduler
seam). It is declared on the **unit**. Three recipes ship: `code` unchanged,
`research`, `synthesize`. Groups are **single-recipe**. Each recipe **prices its
own units**. Verification is **schema-always plus the existing intensity dial**.
Merge is **per-recipe**. The executor seam is a **dispatcher, with the shared
plumbing extracted first** — the primary risk in the plan. A **run-level
Artifact Manifest** is the handoff channel. Export changes are **additive** — no
`schema_version = 3`. And **the pipeline is human-planned; agents never create
nodes.**

### Still unknown — do not assume these were answered

- Whether anyone has published a unit-kind registry **since May 2026** that
  neither survey caught. Both surveys were thorough but the field moves fast.
- The Anthropic per-model **enqueued-token** limit for batches.
- LangGraph `Send` — no 2026-dated first-party confirmation surfaced, though
  nothing contradicted it either.
- CrewAI's exact current version (the Flows DSL is being *formalized*, not
  replaced — `@start`/`@listen`/`@router` are stable).
- DSPy's current assertions / `Refine` / `BestOfN` API, and LiteLLM's budget
  config keys — both out of scope for this round and still unverified.
- Whether R9's extraction from `review.py` can be done without behaviour change
  — an empirical question for the plan, not a research question.

______________________________________________________________________

## 10. Source index

**Registries and typed nodes:** Goose recipes
<https://goose-docs.ai/docs/guides/recipes/recipe-reference/> · deepagents
`subagents.py`
<https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py>
· Roo Code modes <https://docs.roocode.com/features/custom-modes> · Claude Code
subagents <https://code.claude.com/docs/en/sub-agents> · OpenHands
<https://github.com/OpenHands/openhands> · gh-aw
<https://github.com/github/gh-aw> · Dify node base
<https://github.com/langgenius/dify/blob/main/api/core/workflow/nodes/base/node.py>
· Argo concepts <https://argo-workflows.readthedocs.io/en/latest/workflow-concepts/>
· Argo executor plugins
<https://argo-workflows.readthedocs.io/en/latest/executor_plugins/> · Flyte
backend plugins
<https://docs.flyte.org/en/latest/user_guide/extending/backend_plugins.html> ·
Flyte gate nodes
<https://www.union.ai/docs/flyte/user-guide/programming/waiting_for_external_inputs/>
· Temporal child workflows <https://docs.temporal.io/child-workflows> ·
GraphBit <https://arxiv.org/html/2605.13848v1> · AgentEval
<https://arxiv.org/html/2604.23581v1> · Gradientsys
<https://arxiv.org/html/2507.06520v1>

**AOrchestra:** <https://arxiv.org/abs/2602.03786> ·
<https://icml.cc/virtual/2026/poster/65930>

**Context and failure:** Anthropic context engineering
<https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents>
· MAST <https://arxiv.org/abs/2503.13657> · Breunig
<https://www.dbreunig.com/2025/06/22/how-contexts-fail-and-how-to-fix-them.html>
· Cognition <https://cognition.com/blog/dont-build-multi-agents> · OpenAI
Agents SDK handoffs <https://openai.github.io/openai-agents-python/handoffs/>

**Dynamic pipelines:** Airflow dynamic task mapping
<https://airflow.apache.org/docs/apache-airflow/stable/authoring-and-scheduling/dynamic-task-mapping.html>
· Airflow HITL
<https://airflow.apache.org/docs/apache-airflow-providers-standard/stable/operators/hitl.html>
· Argo loops <https://argo-workflows.readthedocs.io/en/latest/walk-through/loops/>
· Argo scaling <https://argo-workflows.readthedocs.io/en/latest/scaling/> ·
Flyte dynamic
<https://docs.flyte.org/en/latest/user_guide/advanced_composition/dynamic_workflows/>
· LangGraph interrupts
<https://docs.langchain.com/oss/python/langgraph/interrupts> · Dagster dynamic
<https://docs.dagster.io/api/dagster/dynamic> · GitLab downstream
<https://docs.gitlab.com/ci/pipelines/downstream_pipelines/> · GitHub Actions
limits <https://docs.github.com/en/actions/reference/limits> · Temporal event
history <https://docs.temporal.io/workflow-execution/event>

**Cost gates:** Infracost <https://www.infracost.io/docs/features/terraform_plan_json/>
· Sentinel `tfrun`
<https://developer.hashicorp.com/terraform/cloud-docs/workspaces/policy-enforcement/import-reference/tfrun>
· BigQuery cost control
<https://docs.cloud.google.com/bigquery/docs/best-practices-costs> · Snowflake
resource monitors <https://docs.snowflake.com/en/user-guide/resource-monitors>
· AWS Budgets actions
<https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-controls.html>
· GitHub environments
<https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments>

**Batch APIs:** <https://claude.com/blog/message-batches-api> ·
<https://developers.openai.com/api/docs/guides/batch> · Ray Data LLM
<https://docs.ray.io/en/latest/data/working-with-llms.html>
