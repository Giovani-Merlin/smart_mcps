"""The Observatory: a local FastAPI app that makes a run legible while it runs
and after it finishes, across projects (plan U2).

Read-only except for one write path — answering a HITL escalation, which
delegates to ``orchestrator.execution.escalation.answer_escalation`` so the CLI
and the UI share a single implementation of that contract.
"""
