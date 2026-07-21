# smart-mcps

Domain glossary for smart-mcps. Opinionated, project-specific terms only.

## Language

**Observatory**:
The local front-end for the orchestrator — a web app that renders an orchestration
run's state from disk and writes back exactly one kind of input, escalation answers.
It observes and answers; it does not launch, abort, or otherwise drive runs.
_Avoid_: dashboard (too generic), control panel (implies launch/abort, which is out of scope)

**Project Registry**:
A single config file listing the target repos the Observatory can switch between,
each as a name plus a repo path. The source of the Observatory's project switcher.
_Avoid_: front-matter (the original loose term for this idea)

**Run Directory**:
The per-run artifact tree at `<repo>/.orchestrator/runs/<run_id>/` (state.json,
manifest.json, groups.json, logs/run.log, escalations/, groups/). The Observatory's
entire read and write surface for a run — there is no other channel. Its
`groups.json` is a per-run snapshot of the shared `.orchestrator/groups.json`,
taken at run start so the DAG survives later re-planning (ADR 0002).

**Run Snapshot**:
The single composed payload the Observatory serves for a run — state.json's group
states and generations, manifest.json's groups→sessions join, and the run's DAG,
merged into one JSON body the SPA renders without further fetches. A read model,
never a stored artifact.
_Avoid_: run state (that is state.json specifically, one of its three inputs)
