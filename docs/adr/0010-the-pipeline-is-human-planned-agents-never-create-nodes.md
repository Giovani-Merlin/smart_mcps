# 0010 — The pipeline is human-planned; agents never create nodes

**Context.** Introducing \[[Unit Recipe]\]s (brainstorm 2026-09-21) makes it
natural to ask whether a `research` unit that finds three candidate strategies
should spawn three implementation units. Every workflow engine has a mechanism
for it — Airflow dynamic task mapping, Argo `withParam`, Flyte `@dynamic`,
Temporal child workflows, LangGraph `Send`, GitLab child pipelines — and
AOrchestra (ICML 2026, arXiv 2602.03786) reports 16.28% over hand-built rosters
by synthesizing sub-agents per task. Our own DAG is computed up front and
snapshotted at run start (ADR 0002), so adopting runtime expansion would be a
deliberate reversal, not an extension.

**Decision.** The human plans the pipeline. **No unit may create, spawn or
expand another unit** — not now, and not when a later \[[Unit Recipe]\] makes it
tempting. A unit instead repeats until it completes or hits a declared cap —
the round budget, `max_rewrites`, the generation cap — and exhausting a cap is a
\[[Work Failure]\], terminal by design so a human acts via \[[Retry]\]. A
recipe whose output attempts to declare new work is an error naming the unit,
never an instruction the orchestrator follows. A research unit writes its
candidates into its artifact; a human re-plans.

**Why.** The published failure modes of letting an LLM shape its own downstream
pipeline are unbounded decomposition, plans that are schema-valid but
operationally nonsense, nondeterministic retry silently changing the work list,
re-planning loops, shared-state races, privilege escalation through generated
definitions, and estimator error. A human-planned DAG makes all seven moot
rather than managed, and the alternative is not a feature but a subsystem:
schema validation, a server-side template allow-list, server-side cost
re-estimation, atomic budget reservation, immutable manifest versioning, and the
discipline that a retry replays the recorded manifest rather than re-prompting.

Two facts decided it. First, nearly every production mechanism above creates
*instances of nodes that already exist in the plan*; only GitLab child pipelines
and Temporal child workflows permit an arbitrary new subgraph, and both push the
whole admission problem onto the caller. Second, the platform caps that would
appear to protect us — Airflow's 1,024 map length, Argo's recursion depth 100,
Temporal's 2,000 incomplete children, LangGraph's 25 supersteps — exist to
protect schedulers and event histories, not budgets; four separate quantities
need capping (fan-out, concurrency, depth, cumulative cost) and we would have to
build all four ourselves.

AOrchestra is conceded where it is actually measured: per-task capability
allocation does beat one-size-fits-all. The answer is that **a recipe defines
the envelope and the policy, and the unit fills in the four-tuple within it** —
a recipe declares a maximum tool set, an allowed model set, a budget policy, a
contract and a merge behaviour; the plan picks specifics inside that envelope.
That keeps auditability, per-recipe budgeting and reproducible replay, which are
what a long autonomous run in someone's repository needs and which the paper
does not measure.

Consequence worth naming: with no automatic expansion, the \[\[Artifact
Manifest\]\] is the only handoff channel between one unit's findings and the
next unit or the human, so it has to work.

Reversing this is a deliberate decision requiring the admission-controller
design recorded in `docs/research/2026-09-21-agent-pipeline-prior-art.md` §5,
not an incremental change.
