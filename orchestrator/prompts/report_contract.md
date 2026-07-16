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

- The `status` attribute is one of completed | blocked | failed and must match the
  JSON body's "status" field.
- Every verification item you were given must appear in "verification_results"
  with status pass | fail | skipped.
- A surprise is a finding that likely invalidates another group's assignment — an
  interface mismatch, a missing dependency, work that belongs elsewhere. Record it
  instead of fixing it yourself:
  {"kind": "interface_mismatch" | "missing_dependency" | "merge_conflict" | "other",
  "description": "...", "affected_groups": ["g2"]}
- The body must be valid JSON. No trailing commas, no comments.
