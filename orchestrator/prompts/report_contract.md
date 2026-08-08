End your final message with EXACTLY ONE report block, after any prose:

<run-report status="completed">
{
  "status": "completed",
  "summary": "one short paragraph of what you did and how it went",
  "verification_results": [
    {"item_id": "<verification item id>", "status": "pass", "notes": ""}
  ],
  "surprises": []
}
</run-report>

Rules for the block:

- The `status` attribute is one of completed | blocked | failed | needs_input |
  permission_denied and must match the JSON body's "status" field.
- Use `needs_input` only when a decision only a human can make blocks you (an
  ambiguous requirement, a product trade-off, missing access). Put the single
  specific question in a top-level "question" field; the run pauses and resumes you
  with the operator's answer. Do not use it for anything you can resolve yourself.
- Use `permission_denied` only after retrying the identical denied command up to
  three times total per the ground rules above. Put the exact command in a
  top-level "denied_command" field, verbatim, with no paraphrasing or quoting
  changes; the group is marked interrupted and resumed once the denial is
  cleared. Do not use it for a `blocked` report — that status is unrelated to
  permission denials and always routes to the escalate-then-rewrite path.
- Every verification item you were given must appear in "verification_results"
  with status pass | fail | skipped.
- A surprise is a finding that likely invalidates another group's assignment — an
  interface mismatch, a missing dependency, work that belongs elsewhere. Record it
  instead of fixing it yourself:
  {"kind": "interface_mismatch" | "missing_dependency" | "merge_conflict" | "other",
  "description": "...", "affected_groups": ["g2"]}
- The body must be valid JSON. No trailing commas, no comments.
