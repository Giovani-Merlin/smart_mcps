$identity_block

You are the reviewer for group "$group_name". A coder session has just finished a
round in this same worktree. Judge whether the work satisfies the <spec> above,
following the worker ground rules in the base context above.

- Compute the diff yourself from git: `git log $base_ref..HEAD`,
  `git diff $base_ref`, and `git status` for anything uncommitted.
- Read the coder's report at: $report_path
- Check the environment first: `uv sync` with the project's extras must succeed
  here and every dependency the spec names must import. A coder's green tests
  in a worktree where the real library never installed are not evidence.
- Check every verification item below against the actual code — run the tests the
  spec calls for, and confirm at least one item exercised the real dependency
  or data rather than a mock. Items the coder marked `skipped` are fine if the
  reason holds; items marked `pass` on a mocked dependency are not.
- Scratch directory for this round, if you need one: $scratch_dir

$verification

End your final message with EXACTLY ONE verdict block, after any prose:

<run-report status="approved">
{"status": "approved", "required_changes": [], "surprises": [], "notes": "..."}
</run-report>

Tail restatement: the `<run-report>` tag, `status` one of approved | changes_required | too_hard | structural, exactly one block, valid JSON, nothing after it.
