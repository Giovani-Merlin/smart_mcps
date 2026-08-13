"""Attributing a `permission_denied` report to a cause (plan P2).

Three unrelated things produced this one status, with three entirely different
remedies: the run's allowlist lacking a rule (add a rule), Landlock denying a
write (widen the write policy), and a genuinely forbidden command (accept it).
The last validation misdiagnosed the first as the second *with the source open*,
which is the cost this module exists to remove.

The rules are ordered, and the ordering is the design, so each test names the
rule it pins rather than merely a happy path.
"""

from __future__ import annotations

from orchestrator.execution.denial import (
    NO_ERROR_TEXT,
    DenialKind,
    classify_denial,
    denial_remedy,
)

# The live text recorded during the validation, verbatim: this is the string the
# classifier most has to get right, since misreading it is the mistake that
# happened.
LIVE_CACHE_EACCES = (
    "Failed to initialize cache at /home/op/.cache/uv: Permission denied (os error 13)"
)


def test_a_kernel_write_denial_is_attributed_to_the_kernel():
    assert (
        classify_denial(denied_command="uv run pytest", denial_error=LIVE_CACHE_EACCES)
        == DenialKind.KERNEL_DENIED
    )


def test_every_errno_rendering_of_the_same_denial_lands_in_one_kind():
    """Same refusal, four runtimes. A classifier that only knew Rust's wording
    would attribute a Python worker's identical failure as UNKNOWN."""
    for text in (
        "OSError: [Errno 13] Permission denied: '/home/op/.npm'",
        "EACCES: permission denied, mkdir '/home/op/.cache'",
        "error: Read-only file system (os error 30)",
        "mkdir: cannot create directory: Operation not permitted",
    ):
        assert classify_denial(denied_command="npm ci", denial_error=text) == (
            DenialKind.KERNEL_DENIED
        ), text


def test_a_harness_refusal_is_attributed_to_the_allowlist():
    assert (
        classify_denial(
            denied_command="npm install --prefix web",
            denial_error="Claude requested permissions to use Bash, but you have not granted it.",
        )
        == DenialKind.HARNESS_ALLOWLIST
    )


def test_denial_source_alone_attributes_a_refusal_with_nothing_quotable():
    """The field's whole justification.

    `tool_refused` encodes the one thing the model knows for free and the
    orchestrator cannot recover: the call never ran. Without it, a refusal the
    model could not quote is indistinguishable from a command that ran and failed.
    """
    assert (
        classify_denial(
            denied_command="npm ci",
            denial_error=NO_ERROR_TEXT,
            denial_source="tool_refused",
        )
        == DenialKind.HARNESS_ALLOWLIST
    )
    assert (
        classify_denial(
            denied_command="npm ci",
            denial_error=NO_ERROR_TEXT,
            denial_source="command_error",
        )
        == DenialKind.KERNEL_DENIED
    )


def test_a_stated_absence_of_error_text_is_not_a_kernel_denial():
    """`no error text was returned` is required of the coder precisely so absence
    is *classifiable*. It must not accidentally match anything."""
    kind = classify_denial(denied_command="something", denial_error=NO_ERROR_TEXT)
    assert kind == DenialKind.UNKNOWN


def test_a_denied_git_mutator_is_policy_forbidden_before_anything_else():
    """First rule, and the only one that needs nothing from the report but the
    command — so it holds even for a coder that quoted nothing.

    It is also the one case where the remedy is "none": a run's deny rules working
    as designed must never look like a misconfiguration to widen.
    """
    kind = classify_denial(
        denied_command="git stash pop",
        deny_rules=["Bash(git stash:*)", "Edit(//home/op/.claude/projects/**/memory/**)"],
    )
    assert kind == DenialKind.POLICY_FORBIDDEN
    assert "no configuration change" in denial_remedy(kind)


def test_policy_forbidden_outranks_an_errno_the_command_also_produced():
    """Ordering, pinned. A forbidden command that *also* hit EACCES is still
    forbidden — widening the write policy would be exactly the wrong response."""
    assert (
        classify_denial(
            denied_command="git push --force",
            denial_error=LIVE_CACHE_EACCES,
            deny_rules=["Bash(git push:*)"],
        )
        == DenialKind.POLICY_FORBIDDEN
    )


def test_an_unrelated_deny_rule_does_not_claim_the_denial():
    assert (
        classify_denial(
            denied_command="npm ci",
            denial_error=LIVE_CACHE_EACCES,
            deny_rules=["Bash(git stash:*)"],
        )
        == DenialKind.KERNEL_DENIED
    )


def test_an_unattributable_denial_says_so_and_names_both_remedies():
    """UNKNOWN has to be honest and useful.

    A confidently wrong attribution is how the original misdiagnosis happened, so
    the fallback must not guess — and must not be a dead end either: it names the
    two things to check.
    """
    kind = classify_denial(denied_command="make build", denial_error="build failed")
    assert kind == DenialKind.UNKNOWN
    remedy = denial_remedy(kind)
    assert "allowed_tools" in remedy and "EACCES" in remedy


def test_the_observed_signal_corroborates_but_is_never_required():
    """`observed` comes from `tool_result` events, whose wording belongs to the
    CLI. It may strengthen a classification and must never be the thing a
    classification depends on — so it feeds only the errno rule, which matches
    libc text rather than CLI prose."""
    # The report alone says nothing; the harness's own output supplies the errno.
    assert (
        classify_denial(
            denied_command="uv sync",
            denial_error=NO_ERROR_TEXT,
            observed=[LIVE_CACHE_EACCES],
        )
        == DenialKind.KERNEL_DENIED
    )
    # And with neither, it stays UNKNOWN rather than defaulting to a guess.
    assert classify_denial(denied_command="uv sync") == DenialKind.UNKNOWN


def test_every_kind_has_a_remedy():
    """The point of attributing is telling an operator what to do, so a kind with
    no remedy is a kind that buys nothing."""
    for kind in DenialKind:
        assert denial_remedy(kind).strip()
