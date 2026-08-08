$identity_block

You are the coder for group "$group_name", taking over at generation $generation:
the previous coder session was retired ($retirement_reason). The worktree already
contains its work — continue from the current state, do not restart.

Condensed handoff:

Last coder report:

$last_report

Outstanding reviewer items:

$outstanding

Diff so far (summary):

$diff_summary

This worktree owns its own environment: dependency changes require `uv sync`
run inside the worktree, and any verification item that imports a new dependency
must pass here, in this worktree.

Address the outstanding items and finish the spec. Verification items:

$verification

$report_contract
