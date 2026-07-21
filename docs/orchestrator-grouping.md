# How grouping works

How `smart-mcps-orchestrate group <plan>` turns a plan document into
`groups.json` — the DAG of execution groups the `run` command later drives.

**At most two LLM calls, everything else deterministic.** The LLM sits only at
the two *edges* (mapper in, speccer out); the whole middle — graph building,
partitioning, sizing, difficulty — is pure Python over codegraph data, seeded
and byte-stable. Plans carrying an embedded **task map**
([`docs/orchestrator-task-map.md`](orchestrator-task-map.md)) skip the mapper
entirely: the planning session already knew the files (including not-yet-existing
ones), symbols, ordering, and slice membership, so LLM #1 becomes a
deterministic parse and only the speccer remains.

The intended authoring flow: `/orchestrator-brainstorm` (requirements doc with
R-IDs) → `/orchestrator-plan` (plan with task map, verifier-checked) →
`smart-mcps-orchestrate group <plan> --dry-run` (human checkpoint) → `run`.

Entry point: [`run_grouping()`](../orchestrator/grouping/pipeline.py) —
`orchestrator/grouping/pipeline.py:54`.

______________________________________________________________________

## Pipeline at a glance

```mermaid
flowchart TD
    plan["plan.md"] --> tmap{"task map<br/>present?"}
    tmap -- "yes (deterministic)" --> parse["parse_task_map()<br/>files/symbols/deps/slices/tags<br/>malformed = hard error"]
    tmap -- no --> mapper

    subgraph LLM1["🧠 LLM call #1 — mapper (fallback only)"]
        mapper["map_tasks()<br/>tasks → files/symbols"]
    end

    mapper --> verify["codegraph verify<br/>drop nonexistent files /<br/>unknown symbols"]
    parse --> verify2["codegraph verify<br/>unknown symbols dropped /<br/>missing files kept as prospective"]
    cg[("codegraph<br/>index")] -.->|callers/callees/impact| verify
    verify --> tg["build_task_graph()<br/>affinity + dependency edges<br/>+ semantic route-tag layer"]
    verify2 --> tg
    cg -.-> tg
    tg --> prose["prose-neighbor fallback<br/>for region-less tasks"]

    prose --> partition

    subgraph DET["⚙️ deterministic core (no LLM)"]
        partition["DefaultPartitionStrategy.partition()<br/>hub → slice contraction → Louvain<br/>→ lift → split → merge"]
        partition --> dag["build_group_dag()<br/>group DAG + cycle check"]
        dag --> est["estimator + difficulty<br/>→ review intensity"]
    end

    est --> speccer

    subgraph LLM2["🧠 LLM call #2 — speccer"]
        speccer["write_specs()<br/>name/summary/spec/verification<br/>per group (never moves tasks)"]
    end

    speccer --> out["groups.json<br/>+ base-context.md"]
```

The sequence in code ([`pipeline.py:54`](../orchestrator/grouping/pipeline.py)):

```python
codegraph_files = client.files_overview()
mapper_out = parse_task_map(plan_text, client)                  # deterministic fast path
if mapper_out is None:
    mapper_out = map_tasks(plan_text, llm_runner, client, ...)  # LLM #1 (fallback)
graph = build_task_graph(mapper_out.mappings, client, weights)  # codegraph + plan-time signals
graph = _with_prose_fallback(graph, mapper_out, ...)            # region-less edges
strategy = DefaultPartitionStrategy(work_fn=..., budget_cap=...)
partition = strategy.partition(graph)                           # deterministic
dag = build_group_dag(graph, partition)
specs = write_specs(plan_text, skeletons, llm_runner, ...)      # LLM #2
# ... assemble Group objects with difficulty/intensity/estimates ...
```

A malformed task map raises `GrouperError` before any LLM call — never a silent
fallback (that would hide drift between the plan prose and the map). An absent
map keeps foreign plans on the mapper path unchanged.

______________________________________________________________________

## The LLM call — what / where / how

Both LLM stages go through **one seam**:
[`call_llm_json()`](../orchestrator/grouping/llm.py) —
`orchestrator/grouping/llm.py:53`. This is the *only* place grouping talks to a
model, which is why tests inject a stub runner and spend zero tokens.

### How the call is physically made

The production runner shells the `claude` CLI in plain print mode with a JSON
schema ([`llm.py:31`](../orchestrator/grouping/llm.py)):

```python
def claude_json_runner(prompt: str, schema: dict) -> str:
    result = subprocess.run(
        ["claude", "-p", prompt,
         "--output-format", "json",
         "--json-schema", json.dumps(schema)],
        capture_output=True, text=True,
    )
    ...
    envelope = json.loads(result.stdout)   # {"result": "...", ...}
    return str(envelope["result"])         # the model's text (JSON string)
```

- **`-p` (print mode):** one blocking, stateless call — no session, no resume.
  Grouping calls are one-shots; the *execution* engine (`sessions.py`) owns the
  session/fork lifecycle, not this.
- **`--json-schema`:** asks the CLI for schema-constrained structured output.
- **`--output-format json`:** the CLI wraps the answer in an envelope; we pull
  `envelope["result"]` (itself a JSON string that we then parse).
- **`JsonRunner`** is a `Callable[[str, dict], str]` — tests pass a canned
  function instead of `claude_json_runner`, so the whole pipeline runs offline.

### Validate → retry → fail-loud

`call_llm_json` parses the returned text, validates it, and retries with a
corrective nudge on failure — capped, then aborts with the raw output saved
([`llm.py:53`](../orchestrator/grouping/llm.py)):

```python
for _attempt in range(1 + max_retries):          # default 1 + 2 = 3 tries
    last_raw = runner(attempt_prompt, schema)
    try:
        return validate(json.loads(last_raw))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        attempt_prompt = f"{prompt}\n\nYour previous output failed validation: {exc}.\n" \
                         "Return ONLY valid JSON matching the schema — no prose, no fences."
# persistent failure → raise LlmError, raw output saved to .orchestrator/failures/
```

```mermaid
flowchart LR
    P["prompt + schema"] --> R["claude -p --json-schema"]
    R --> J{"valid JSON<br/>& passes<br/>validate()?"}
    J -- yes --> OK["typed result"]
    J -- "no (< 3 tries)" --> N["re-prompt with<br/>the error"] --> R
    J -- "no (3rd try)" --> F["LlmError +<br/>save raw to<br/>.orchestrator/failures/"]
```

______________________________________________________________________

## LLM #1 — the mapper (plan → code regions)

[`map_tasks()`](../orchestrator/grouping/mapper.py) —
`orchestrator/grouping/mapper.py:70`. **What it does:** extract implementation
tasks from the plan and map each to the files/symbols it will touch.

- **Prompt:** [`orchestrator/prompts/mapper.md`](../orchestrator/prompts/mapper.md),
  filled with `$plan_text` and `$codegraph_files` (the repo's file/symbol
  overview from `client.files_overview()`). It instructs: one task per unit of
  work, in plan order, kebab-case `task_id`, and *"only name files/symbols you are
  confident about — the orchestrator verifies every path and drops misses."*
- **Schema:** `MAPPER_SCHEMA` — `tasks[]` of `{task_id, description, files[], symbols[]}`.
- **Then, deterministically, every region is verified against codegraph and the
  working tree** ([`mapper.py:94`](../orchestrator/grouping/mapper.py)):

```python
for file in entry["files"]:
    if (client.repo_root / file).is_file(): files.append(file)
    else: flags.append(f"... mapped nonexistent file {file} — dropped")
for symbol in entry["symbols"]:
    if client.symbol_exists(symbol): symbols.append(symbol)
    else: flags.append(f"... mapped unknown symbol {symbol} — dropped")
if not files and not symbols:
    flags.append(f"... task {task_id} has no verifiable regions — carried as region-less node ...")
```

> ⚠️ **This drop step is the greenfield weak point of the mapper path.** On a
> plan for code that doesn't exist yet, *every* mapped file is "nonexistent →
> dropped", so tasks become region-less and lose all structural signal.
> **Resolved for pre-mapped plans:** the task-map parser keeps such files as
> *prospective files* instead of dropping them — they contribute shared-file
> affinity, appear in `Group.files`, and count in the per-file token allowance.
> See [Known limitations](#known-limitations).

______________________________________________________________________

## The task graph (deterministic, codegraph-driven)

[`build_task_graph()`](../orchestrator/grouping/graphing.py) —
`orchestrator/grouping/graphing.py:162`. It queries codegraph for every mapped
symbol and assembles a [`TaskGraph`](../orchestrator/grouping/partition.py) with
**two relations**:

| Relation                    | Kind      | Weight (default)                              | Source                                                  |
| --------------------------- | --------- | --------------------------------------------- | ------------------------------------------------------- |
| **affinity** (clustering)   | symmetric | `shared_file` 1.0 · `call` 2.0 · `impact` 1.5 | tasks touching same file / calling / impact-overlapping |
| **dependencies** (ordering) | directed  | same edges, kept directed                     | caller-task ⟶ callee-task, impacted ⟶ impacting         |

Pre-mapped plans add three **plan-time signals** on top
([`docs/orchestrator-task-map.md`](orchestrator-task-map.md)):

| Signal                | Becomes                                                                              | Notes                                                                                                                                                                                |
| --------------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **prospective files** | shared-file affinity (union with real files)                                         | the greenfield fix — planned-but-nonexistent files cluster like real ones; 0 source bytes, full per-file allowance                                                                   |
| **`depends_on`**      | directed dependency edge only — **never affinity**                                   | feeds the group DAG, hub detection (a scaffold everyone depends on → `utility_hub` → own group, scheduled first), merge-direction guards                                             |
| **route tags**        | symmetric **semantic affinity** between matched `implements`/`consumes` (weight 1.5) | the cross-stack fix (TS `fetch("/api/x")` ↔ Python route); the whole layer is scaled by `clamp(Σw_struct/Σw_sem, semantic_floor 0.5, semantic_ceil 3.0)` so it rebalances per regime |

The clamp is the regime balancer (multilayer-modularity practice): pure
greenfield (Σstruct≈0) floors the scale and semantics dominate the near-empty
structural layer; edit-heavy plans hit the ceiling so semantics refine but never
override real reference edges; mixed plans land between. Knobs: `semantic`,
`semantic_floor`, `semantic_ceil` on `EdgeWeightsConfig`.

```mermaid
flowchart LR
    subgraph signals["codegraph signals → edges"]
        direction TB
        SF["two tasks touch the<br/>same file"] -->|"affinity +1.0"| E
        CALL["task A's symbol calls<br/>task B's symbol"] -->|"affinity +2.0<br/>dep A→B"| E
        IMP["task A's change<br/>impacts task B's code"] -->|"affinity +1.5<br/>dep A→B"| E
        E["TaskGraph<br/>affinity + dependencies"]
    end
    PN["region-less tasks:<br/>plan-order neighbor"] -->|"affinity +0.5"| E
```

The edge-building loop ([`graphing.py:193`](../orchestrator/grouping/graphing.py)):

```python
# Shared-file affinity: weight scales with the number of files two tasks share.
for _file, owners in sorted(file_owner.items()):
    for a, b in pairs(sorted(owners)):
        edges.add_symmetric(a, b, weights.shared_file)

for symbol in mapping.symbols:                    # per mapped symbol
    for caller in client.callers(symbol):         # call proximity (directed)
        for other in owners_of(caller) - {task}:
            edges.add(upstream=task, downstream=other, weight=weights.call)
    for affected in client.impact(symbol):        # impact overlap (directed)
        for other in owners_of(affected) - {task}:
            edges.add(upstream=task, downstream=other, weight=weights.impact)
```

**Region-less fallback.** Tasks the mapper couldn't map get one weak edge to
their plan-order neighbor — the *only* non-codegraph signal
([`pipeline.py:175` `_with_prose_fallback`](../orchestrator/grouping/pipeline.py),
weight `prose_neighbor` = 0.5). This is all that survives for a greenfield plan.

Edge weights live in config ([`EdgeWeightsConfig`](../orchestrator/config.py),
`config.py:16`) — all overridable.

______________________________________________________________________

## The partition (deterministic, CoCoder-ported)

[`DefaultPartitionStrategy.partition()`](../orchestrator/grouping/partition.py) —
`orchestrator/grouping/partition.py:157`. A fixed sequence of policies ported from
CoCoder (Apache-2.0), behind a swappable `PartitionStrategy` interface (R22):

```mermaid
flowchart TD
    A["detect_hub_roles()<br/>utility_hub / aggregator_hub / core"] --> S
    S["_slice_atoms() → _contract_slices()<br/>each slice's CORE members become<br/>one supernode (hubs never absorbed)"] --> B
    B["_hub_isolated_clustering()<br/>Louvain communities on the CORE<br/>(seeded, resolution-tuned,<br/>slice self-loops preserved)"] --> C
    C["lift_independent()<br/>split siblings that only depend<br/>on internal hubs (unit-level)"] --> X
    X["expand slice supernodes<br/>back to their member tasks"] --> D
    D["split_over_budget()<br/>break groups over the token cap<br/>at their lowest-affinity boundary<br/>(may break an oversized slice)"] --> E
    E["merge_small_groups()<br/>merge tiny groups (dep-safe,<br/>makespan no-regression)"] --> F
    F["_renumber() → build_group_dag()<br/>contiguous ids; cycle = loud failure"]
```

**Slice contraction** (task-map must-links): after hub roles are fixed, each
slice's core members contract into a `slice::<label>` supernode — sorted
iteration, parallel edge weights summed, intra-slice affinity kept as a Louvain
self-loop (Rey et al. 2022: contracted-graph modularity ≡ constraint-respecting
original modularity). Clustering and sibling-lifting run at unit level, then
membership expands. The must-link is hard through clustering but soft after:
`split_over_budget` may still break an oversized slice at its weakest internal
edges, and `merge_small_groups` may combine small ones. A slice-induced group
cycle fails loudly at DAG build, naming the offending task edges — the plan
skill's verifier requires inter-slice `depends_on` acyclicity up front.

- **Hub roles** ([`partition.py:171`](../orchestrator/grouping/partition.py)):
  degree-thresholded — a node most others depend on (`utility_hub`) is isolated as
  its own group; a node depending on most others (`aggregator_hub`) trails in one
  shared group; the rest are `core`.
- **Louvain** ([`partition.py:201`](../orchestrator/grouping/partition.py)):
  `networkx` directed Louvain over the affinity weights, **seeded** (`LOUVAIN_SEED = 42`) with deterministic community numbering → stable across runs.
- **lift / split / merge** ([`partition.py:261/328/462`](../orchestrator/grouping/partition.py)):
  peel off independent siblings, split any group whose summed **token work**
  exceeds the budget cap ([estimator hook](#estimator--difficulty--review-tier)),
  merge undersized groups without creating a dependency cycle or regressing
  makespan.
- **DAG + cycle check** ([`build_group_dag()` `partition.py:91`](../orchestrator/grouping/partition.py)):
  lifts task dependencies to group-level edges; a cycle raises `GroupCycleError`
  naming the offending task edges (CoCoder silently wedged here — we fail loudly).

> **What the partition optimizes: affinity modularity + a token budget + makespan
> no-regression. What it does _not_ model: testability, vertical slices, or
> cross-stack cohesion.** A group is "a cluster of tightly-coupled code," not "an
> independently-shippable/testable unit." See [Known limitations](#known-limitations).

______________________________________________________________________

## Estimator → difficulty → review tier

Two deterministic scorers ([`orchestrator/grouping/estimator.py`](../orchestrator/grouping/estimator.py)),
both reading plain numbers so the partitioner consumes them as injected hooks:

- **Token estimate / budget** — `node_work()` (per-task tokens from source bytes +
  per-file allowance) feeds the partition's `budget_cap`; `estimate_group_tokens()`
  sizes the final group (`estimator.py:19/36/47`).
- **Difficulty → review intensity** — `difficulty_score()` is a normalized weighted
  sum of saturating signals (files touched, max fan, hub touches, cross-group
  edges, verification count); `intensity_for()` maps it to a tier
  ([`estimator.py:76/103`](../orchestrator/grouping/estimator.py)):

```python
if difficulty < config.d_review:  return SELF_VERIFY   # no reviewer session
if difficulty < config.d_hard:    return PAIRED        # one reviewer
return PAIRED_PLUS                                      # + mandatory extra pass
```

Thresholds/weights all live in [`DifficultyConfig`](../orchestrator/config.py)
(`config.py:45`).

______________________________________________________________________

## LLM #2 — the speccer (prose about fixed groups)

[`write_specs()`](../orchestrator/grouping/speccer.py) —
`orchestrator/grouping/speccer.py:62`. **What it does:** for each already-decided
group, write the `name`, `summary` (≤120 chars, the analyzer title), worker-facing
`spec`, and `verification` items. **It never moves tasks or invents groups.**

- **Prompt:** [`orchestrator/prompts/speccer.md`](../orchestrator/prompts/speccer.md),
  filled with `$plan_text` and `$groups_json` (the skeleton: each group's tasks,
  descriptions, files).
- **Schema:** `SPECCER_SCHEMA` — `groups[]` of `{group_id, name, summary, spec, verification[]}`.
- **Validation** ([`speccer.py:79`](../orchestrator/grouping/speccer.py)) rejects a
  summary over 120 chars (never truncates) and requires the output to cover
  *exactly* the given group ids.

> **Note for the next session:** the speccer can *see* cross-group ordering in its
> prose (in the live smoke it wrote *"this group builds on the scaffold group"*),
> but that understanding is **not fed back into the DAG** — the structural
> `dependencies` were already fixed upstream. Bridging that prose→DAG gap is one
> lever for the greenfield problem.

______________________________________________________________________

## Output

[`serialize_grouping()`](../orchestrator/grouping/pipeline.py) writes:

- **`.orchestrator/groups.json`** — the canonical `GroupingResult`: every `Group`
  with id, name, summary, spec, difficulty, intensity, **dependencies**,
  verification, tasks, files, estimated_tokens, plus `flags` (dropped mappings,
  budget warnings). This is what `run` consumes.
- **`.orchestrator/base-context.md`** — repo conventions (`CLAUDE.md`/`AGENTS.md`) +
  codegraph architecture summary + the plan, compiled once as the base session's
  shared context ([`base_context.py:16`](../orchestrator/grouping/base_context.py)).

`--dry-run` prints the report and skips both writes — the human checkpoint before
any session launches.

______________________________________________________________________

## Where the tokens go

| Stage                               | LLM? | Call                                         | Count per `group`                                              |
| ----------------------------------- | ---- | -------------------------------------------- | -------------------------------------------------------------- |
| Task-map parse                      | ❌   | pure Python (pyyaml + codegraph verify)      | 0                                                              |
| Mapper                              | ✅   | `claude -p --json-schema` (`MAPPER_SCHEMA`)  | 1 (+ up to 2 retries) — **skipped when a task map is present** |
| Build graph / partition / estimator | ❌   | codegraph CLI + pure Python                  | 0                                                              |
| Speccer                             | ✅   | `claude -p --json-schema` (`SPECCER_SCHEMA`) | 1 (+ up to 2 retries)                                          |

So a `group` run is **two model calls** on the happy path — **one** for a
pre-mapped plan; everything structural is deterministic and offline. (The `run`
command that follows is where the many coder/reviewer sessions and their tokens
live.)

______________________________________________________________________

## Known limitations

These drove the next-session focus — full write-up in
[`docs/handoffs/2026-07-16-multiagent-orchestrator-phase-d-and-grouping-next.md`](handoffs/2026-07-16-multiagent-orchestrator-phase-d-and-grouping-next.md).

1. **Greenfield loses structure — ✅ resolved for pre-mapped plans.** The mapper
   drops mappings to files that don't exist yet, so a plan for new code collapses
   to region-less tasks → only the weak prose-neighbor affinity survives → no
   clustering, no dependency ordering. (Observed live: 3 independent groups that
   actually needed a scaffold→consumer chain.) A task map retains those files as
   **prospective files** (full affinity + `Group.files` + per-file allowance) and
   its `depends_on` restores ordering; the mapper path keeps the old behavior for
   foreign plans. Regression fixture:
   `tests/test_grouper_pipeline.py::TestTaskMapRegimes`.
2. **No testability / vertical-slice objective — ✅ resolved for pre-mapped
   plans.** Grouping is purely structural affinity; codegraph has no edge between,
   say, a TS `fetch()` and its Python route, so cross-stack halves of one feature
   fragment into separately-reviewed groups. A task map's `slice` labels
   (must-link node contraction) plus matched `implements`/`consumes` route tags
   (semantic affinity layer) keep a feature's halves together. Plans without a
   task map still have no semantic signal.
3. **LLM non-determinism.** Mapper + speccer are model calls, so group boundaries
   can differ run-to-run for the same plan (dry-run made 3 groups, the real run
   made 2). A pre-mapped plan removes the mapper's share of this entirely (the
   parse is byte-stable); the speccer still writes prose but **never moves
   tasks**. `--dry-run` is the intended human checkpoint.

```
```
