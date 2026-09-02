# 0008. Run reports are generated from run artifacts; an LLM only fills validated slots

**Context.** Every human-facing write-up of a run was authored freely by the
run-driver session, and the result was consistently unread: 200–470-line
notes files, restated requirements, "tests pass" with no evidence. The
facts a human needs (what landed, what proved it, what broke, what it cost)
already exist as JSON under the run directory.

**Decision.** `smart-mcps-orchestrate report` renders every human-facing
document (changelog entry, HTML report, PR body, postmortem-lite) from the
run artifacts and git, with zero LLM calls. The only LLM-authored piece is a
one-pager written under a hard contract (fixed headings, bullet and word
caps, every bullet ending in an artifact pointer) that the same CLI
validates and rejects. The run driver may write it; it may not write the
record.

**Why.** Free narrative is the failure mode, not the template text; a
validator that fails the step is what keeps a later session from drifting
back. Deterministic rendering also means the report cannot disagree with the
Observatory, which reads the same snapshot.
