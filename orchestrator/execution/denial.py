"""Attributing a ``permission_denied`` report to a cause (plan P2).

Three unrelated things produced the same status and the same opaque
``denied_command``:

1. the run's ``--allowedTools`` (or the operator's settings) lacked a rule, so the
   harness refused the call before it ran;
2. Landlock denied a write the command actually attempted;
3. the command is on ``--disallowedTools`` and is genuinely forbidden.

The remedies are entirely different — add an allow rule, widen the write policy,
or accept the refusal — and the last validation misdiagnosed (1) as (2) with the
source open. Splitting the *status* was the obvious fix and the wrong one: one
status is what the review loop, the scheduler and the UI all agree on today, and
three would multiply that agreement by three for information that is really an
attribute of one outcome. So the status stays single and the cause is classified.

This module deliberately imports from nothing in ``execution/`` — same reasoning
as ``PermissionDenied`` living in ``model.py``: ``review.py`` imports
``scheduler.py``, and the Observatory's artifact reader calls the classifier too,
so a classifier that reached into either would be unimportable from one of its two
callers.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from enum import StrEnum


class DenialKind(StrEnum):
    """What actually blocked the command, and therefore what to do about it."""

    #: The permission layer refused the call — it never ran. Remedy: an
    #: `allowed_tools` rule on the run.
    HARNESS_ALLOWLIST = "harness_allowlist"
    #: The command ran and the *kernel* refused a write. Remedy: `cache_root` /
    #: `extra_write_paths`, or the work genuinely belongs inside the worktree.
    KERNEL_DENIED = "kernel_denied"
    #: The command matches the run's deny list. Not a misconfiguration — the
    #: answer is usually that the coder must find another way.
    POLICY_FORBIDDEN = "policy_forbidden"
    #: Not attributable from what was reported. Must say so and name both
    #: remedies rather than guess: a confidently wrong attribution is how the
    #: original misdiagnosis happened.
    UNKNOWN = "unknown"


#: Kernel/libc signatures — the command ran and the filesystem said no. The first
#: is the live text recorded during the validation (`Failed to initialize cache at
#: ~/.cache/uv … Permission denied (os error 13)`); the rest are the same errno
#: rendered by other runtimes.
_KERNEL_PATTERNS = (
    r"os error 13\b",
    r"\bEACCES\b",
    r"\[Errno 13\]",
    r"\bEPERM\b",
    r"errno 13\b",
    r"read-only file system",
    r"operation not permitted",
    r"permission denied \(os error",
)

#: Permission-layer wording. Narrower on purpose: a bare "permission denied" is
#: ambiguous between the two worlds, so it is *not* here — the kernel patterns
#: above claim the errno-shaped ones, and anything left over is UNKNOWN rather
#: than guessed.
_HARNESS_PATTERNS = (
    r"requires? (?:approval|permission)",
    r"not (?:in the )?allow(?:ed|list)",
    r"tool use was (?:rejected|denied|blocked)",
    r"user (?:rejected|denied)",
    r"permission to use",
    r"claude requested permissions",
    r"has not been granted",
)

_KERNEL_RE = re.compile("|".join(_KERNEL_PATTERNS), re.IGNORECASE)
_HARNESS_RE = re.compile("|".join(_HARNESS_PATTERNS), re.IGNORECASE)

#: What a coder is told to write when there was genuinely nothing to quote. A
#: stated absence is classifiable (it rules out both quoted-text rules); a blank
#: field is indistinguishable from a coder that did not bother.
NO_ERROR_TEXT = "no error text was returned"


def classify_denial(
    *,
    denied_command: str,
    denial_error: str = "",
    denial_source: str = "",
    deny_rules: Sequence[str] = (),
    observed: Sequence[str] = (),
) -> DenialKind:
    """Attribute a denial, from the report plus whatever the orchestrator saw.

    Rules are ordered by how much they depend on the model having quoted anything:

    1. ``POLICY_FORBIDDEN`` — the command matches the run's own deny rules. First
       because it is the only rule that needs nothing from the report but the
       command itself, and it is the one case where the operator's remedy is
       "none, this is working as intended".
    2. ``KERNEL_DENIED`` — an errno signature in the quoted error or in what the
       orchestrator observed on the wire. Before the harness rule because errno
       text is unambiguous where refusal wording is not.
    3. ``HARNESS_ALLOWLIST`` — ``denial_source == "tool_refused"`` (the model
       telling us the call never ran, which the orchestrator cannot recover) or
       permission-layer wording.
    4. ``UNKNOWN`` otherwise.

    ``observed`` is a **corroborator, never the sole source**: it is collected
    passively from ``user``/``tool_result`` stream events, whose exact wording is
    the CLI's to change, so a classification must never rest on it alone — hence
    it only ever contributes to the errno rule, which matches libc text rather
    than CLI prose.
    """
    if denied_command and _matches_deny_rules(denied_command, deny_rules):
        return DenialKind.POLICY_FORBIDDEN

    haystack = "\n".join([denial_error, *observed])
    if _KERNEL_RE.search(haystack):
        return DenialKind.KERNEL_DENIED
    if denial_source == "tool_refused":
        return DenialKind.HARNESS_ALLOWLIST
    if _HARNESS_RE.search(haystack):
        return DenialKind.HARNESS_ALLOWLIST
    if denial_source == "command_error":
        # The model says the command *ran* and was refused, but quoted nothing an
        # errno pattern matches. Last, so quoted refusal wording still wins: this
        # is the weakest of the three signals, and the only one with no text
        # behind it.
        return DenialKind.KERNEL_DENIED
    return DenialKind.UNKNOWN


def _matches_deny_rules(denied_command: str, deny_rules: Sequence[str]) -> bool:
    """Whether ``denied_command`` is covered by any ``--disallowedTools`` rule.

    Rules look like ``Bash(git stash:*)`` or ``Edit(//path/**)``. The reported
    command is free-form prose from a model, so this matches the rule's *inner*
    pattern against it rather than trying to reconstruct a tool invocation — a
    coder that reports `git stash pop` against a `Bash(git stash:*)` rule must be
    attributed, and demanding the exact tool syntax would attribute nothing.
    """
    text = denied_command.strip()
    for rule in deny_rules:
        inner = _rule_pattern(rule)
        if not inner:
            continue
        if fnmatch.fnmatch(text, inner) or text.startswith(inner.rstrip("*").rstrip(":")):
            return True
    return False


def _rule_pattern(rule: str) -> str:
    """``Bash(git stash:*)`` → ``git stash:*``; a bare ``Bash`` → ``""``."""
    start = rule.find("(")
    if start < 0 or not rule.endswith(")"):
        return ""
    return rule[start + 1 : -1].strip()


#: What to tell the operator per kind — the remedy, which is the whole point of
#: attributing. UNKNOWN names *both* remedies rather than picking one.
_REMEDIES: dict[DenialKind, str] = {
    DenialKind.HARNESS_ALLOWLIST: (
        "the permission layer refused the call before it ran — add a matching rule "
        "to [session] allowed_tools"
    ),
    DenialKind.KERNEL_DENIED: (
        "the command ran and the kernel refused a write — the target is outside the "
        "worktree; widen [session] extra_write_paths or move the work inside it"
    ),
    DenialKind.POLICY_FORBIDDEN: (
        "the command matches this run's deny rules and is forbidden by design — no "
        "configuration change is wanted"
    ),
    DenialKind.UNKNOWN: (
        "not attributable from what was reported — check both the run's "
        "allowed_tools (a refused call never runs) and the write policy (a running "
        "command hitting EACCES)"
    ),
}


def denial_remedy(kind: DenialKind) -> str:
    return _REMEDIES[kind]
