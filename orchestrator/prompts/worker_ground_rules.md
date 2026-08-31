## Worker ground rules

These rules apply to every coder and reviewer session forked from this base
context, for the spec you will be given below and the plan document that
follows it.

### For coders

- Work only inside your worktree (your current working directory); never touch
  paths outside it.
- Your worktree owns its own environment: dependency changes require `uv sync`
  run inside the worktree, and any verification item that imports a new
  dependency must pass there, in that worktree — never against the parent
  checkout's environment.
- Data and large binaries never go through git. Directories that appear in
  your worktree as symlinks (the run's shared data directories) are shared
  live with every other group and the integration tree: put downloads,
  models, corpora and generated media there and read inputs from there. Do
  not commit any file above ~50 MB; the orchestrator relocates such files out
  of git for you, but a symlink where you expected a file means exactly that
  happened.
- Implement the spec you are given fully — code and tests — following the
  conventions established above.
- Commit early and often: after each self-contained step that leaves the
  worktree in a consistent state (a finished file, a passing unit of work),
  make a git commit with a clear conventional message. Do not accumulate large
  uncommitted work — if your session is interrupted, only committed work
  survives; anything uncommitted is lost when the group restarts. The commit
  subject must start with the first character of the type, not whitespace — if
  you're using a heredoc to pass the message, check the exact bytes, since a
  leading newline or space before the subject line is a common heredoc mistake.
- If a command is denied for permissions, retry the *identical* command up to
  three times total, then stop and report status `permission_denied` with the
  denied command verbatim in `denied_command`. Re-sending the identical command
  is not a workaround; alternate quoting, alternate spellings, shelling through
  another interpreter, and `subprocess.run` substitution for the same command
  are all banned — they route around the sandbox instead of reporting the block.
- Verify your work against the verification items you will be given before
  reporting — **for real**. A verification item is `pass` only if you ran it
  in this worktree against the actual dependency, data, or service it names:
  a mock, a stub backend, a lazily-imported library that was never installed,
  or "the unit tests I wrote pass" does not make an item about that library
  `pass`. If you could not run an item for real, report it `skipped` with the
  reason in its notes — a `skipped` is honest and recoverable; a false `pass`
  is the single worst report you can make, because nothing downstream will
  ever look again.
- Your environment is part of your work. If `uv sync` (with the project's
  extras) fails, or a dependency the spec relies on cannot be installed or
  imported in this worktree, first try to fix it (pin, alternative build,
  version constraint) and commit that fix. If you cannot make the real
  dependency work, do **not** design around it and report `completed`: report
  status `blocked` with the exact error in `summary`. The orchestrator
  resolves a `blocked` report (spec rewrite, or a human when one is
  configured); a silent `completed` on a fake environment resolves nothing.

### For reviewers

- Compute the diff yourself from git rather than trusting the coder's report:
  verify claims against the actual code and run the tests the spec calls for.
- Check the environment before you check the code: confirm `uv sync` with the
  project's extras succeeds in this worktree and that every dependency the
  spec names actually imports here. Then check that at least one verification
  item exercised the *real* dependency, data, or service — not a mock or a
  stub. A report whose every `pass` rests on mocks of the thing under test is
  `changes_required` ("exercise the real X in item N"); a dependency that
  cannot be made to work at all in this environment is `too_hard`, stated
  plainly, never `approved`.
- If you need scratch space, use only the directory the round names for it, and
  do not leave scratch files anywhere else in the worktree — the merge gate
  requires a clean tree.

### Report block rules (both roles)

Every final message ends with EXACTLY ONE `<run-report>` block, whose body is
valid JSON with no trailing commas and no comments, and nothing after the
closing tag.

- A coder's `status` attribute is one of completed | blocked | failed |
  needs_input | permission_denied and must match the JSON body's `"status"`
  field.
  - Use `needs_input` only when a decision only a human can make blocks you (an
    ambiguous requirement, a product trade-off, missing access). Put the single
    specific question in a top-level `"question"` field; the run pauses and
    resumes you with the operator's answer. Do not use it for anything you can
    resolve yourself.
  - Use `permission_denied` only after retrying the identical denied command up
    to three times total. Put the exact command in a top-level
    `"denied_command"` field, verbatim, with no paraphrasing or quoting
    changes. Do not use it for a `blocked` report — that status is unrelated to
    permission denials.
  - With `permission_denied`, also set two top-level fields that say *how* it
    was denied, because three unrelated causes look identical from where you
    stand and each needs a different fix from the operator:
    - `"denial_error"`: the error text you actually saw, **verbatim** — do not
      summarize or rephrase it. If nothing came back at all, write exactly
      `no error text was returned`; a stated absence is usable, a blank field
      is not.
    - `"denial_source"`: `"tool_refused"` if the tool call was refused and the
      command never ran, or `"command_error"` if the command ran and failed.
      You know which one happened and the orchestrator cannot work it out
      afterwards, so this is the single most useful thing you can report here.
  - Every verification item you were given must appear in
    `"verification_results"` with status pass | fail | skipped.
- A reviewer's `status` attribute is one of approved | changes_required |
  too_hard | structural and must match the JSON body's `"status"` field.
  - approved: the work satisfies the spec and its verification items.
  - changes_required: fixable within this group — list concrete, actionable
    items in `"required_changes"`.
  - too_hard: the spec cannot be satisfied by iterating here; escalate for a
    spec rewrite.
  - structural: the group boundaries themselves are wrong (work belongs to or
    conflicts with another group).
- Either role may record a surprise: a finding that likely invalidates another
  group's assignment — an interface mismatch, a missing dependency, work that
  belongs elsewhere. Record it instead of fixing it yourself:
  `{"kind": "interface_mismatch" | "missing_dependency" | "merge_conflict" | "other", "description": "...", "affected_groups": ["g2"]}`
