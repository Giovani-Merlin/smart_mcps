# 0006 — Delete the grouping-time speccer; specs are assembled deterministically

**Context.** The grouping pipeline's only mandatory LLM call was the
grouping-time speccer (`write_specs`): one Opus call, ~65k input / 29k output
tokens, 284 s, whose output measured ~90% a restatement of the plan's own unit
sections. The plan is written by an interactive LLM session
(`/orchestrator-plan`) — from that point of view the planner *is* the speccer.

**Decision.** The grouping-time speccer is deleted outright (module, prompt
template, CLI/Observatory grouping-form model knob) in one standalone commit so
it can be recovered by cherry-pick. Group specs are assembled deterministically
from the plan's unit sections plus graph facts. The **mid-run rewrite speccer**
(escalation-driven spec rewrite for failed groups) and the Observatory's
LLM-call viewer stay — the rewrite consumes genuinely new information (the
group's failure history), which assembly cannot.

**Why.** Correct facts beat fluent paraphrase for LLM workers; the paraphrase
added cost, latency, and a drift/hallucination surface, not information.
Keeping it opt-in was rejected: enrichment belongs in the plan doc (the deepen
skill, wave 2), where it survives re-grouping, so an opt-in overlay would only
produce specs that die on the next re-group.
