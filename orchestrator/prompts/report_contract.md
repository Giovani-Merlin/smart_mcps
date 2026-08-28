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

Tail restatement: the `<run-report>` tag, `status` one of completed | blocked |
failed | needs_input | permission_denied, exactly one block, valid JSON,
nothing after it.
