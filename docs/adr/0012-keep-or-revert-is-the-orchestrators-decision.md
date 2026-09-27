# 0012 — Keep-or-revert is the orchestrator's decision; a judge is a harness script

**Context.** The `optimize` \[[Unit Recipe]\] (brainstorm 2026-09-27) starts
from working code and improves a measured KPI. Karpathy's `autoresearch` lets
the agent decide `lower → keep`, and its own README graph ends on a random-seed
change accepted as an improvement (issue #466); the 2026 reward-hacking
literature puts prompt-only mitigation at ~6pp. Infinity Skills already runs a
pairwise LLM-judge tuning loop whose judge lives inside its eval code.

**Decision.** The orchestrator decides every candidate's outcome from numbers
in the evaluate child's measurements JSON — \[[Keep-or-Revert]\] with a noise
floor from evaluations already paid for — and the coder is told the outcome,
never asked for it. The orchestrator never computes a metric and never calls
a judge: an LLM-as-judge KPI is a script inside the protected
\[[Evaluation Harness]\] that makes its own LLM calls and writes the same JSON.
The mutable region is the unit's `files:`; harness paths are hash-checked
before every scoring run.

**Why.** One accept rule then covers scripts and judges; the judge prompt sits
where the optimizer cannot edit it; and a keep is trustworthy without n≥3
repeats per candidate, which the 20-evaluation budget cannot afford. The price
accepted: a judge's cost is a recipe child's and reads "unknown", and a
deterministic KPI reduces the rule to a declared minimum effect.
