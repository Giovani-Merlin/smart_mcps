"""Configuration surface: thresholds, weights, and defaults for every stage.

All values are config-overridable (origin R5: thresholds are configuration, never
hardcoded policy). Defaults must load without any config file present; U9 layers
CLI-flag > config-file > default resolution on top of `load_config`.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

#: Baseline `--allowedTools` for a worker: the toolchains a coder has to drive to
#: build, test and commit. File tools are listed explicitly because `acceptEdits`
#: covers edits but not reads or searches.
#:
#: Deny still wins — `disallowed_tools` keeps the repo-global git mutators and the
#: operator-memory rules blocked regardless of what appears here.
#:
#: Each executable rule is paired with a `*/`-prefixed twin by
#: `_with_path_qualified_forms`, because a rule matches the command *as written*
#: and one program has many names: `python` and `.venv/bin/python` are the same
#: interpreter but different strings. Group g2 of run r20260812-202855 died on
#: `.venv/bin/python -m pytest …` with `Bash(python *)` sitting right there in
#: this list — the npm incident's structural twin, a missing *spelling* rather
#: than a missing tool.
_BASE_ALLOWED_TOOLS: tuple[str, ...] = (
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "Glob",
    "Grep",
    "TodoWrite",
    "NotebookEdit",
    "Bash(git *)",
    "Bash(uv *)",
    "Bash(python *)",
    "Bash(python3 *)",
    "Bash(pytest*)",
    "Bash(ruff*)",
    "Bash(mypy*)",
    "Bash(pip *)",
    "Bash(pip3 *)",
    "Bash(npm *)",
    "Bash(npx *)",
    "Bash(node *)",
    "Bash(pnpm *)",
    "Bash(yarn *)",
    "Bash(make *)",
    "Bash(ls *)",
    "Bash(cat *)",
    "Bash(head *)",
    "Bash(tail *)",
    "Bash(find *)",
    "Bash(rg *)",
    "Bash(grep *)",
    "Bash(sed *)",
    "Bash(awk *)",
    "Bash(mkdir *)",
    "Bash(cp *)",
    "Bash(mv *)",
    "Bash(echo *)",
    "Bash(cd *)",
    "Bash(test *)",
    "Bash(which *)",
    "Bash(env)",
)


def _with_path_qualified_forms(rules: tuple[str, ...]) -> tuple[str, ...]:
    """Pair every `Bash(<cmd>…)` rule with a `Bash(*/<cmd>…)` twin.

    A worker legitimately invokes tools by path — `.venv/bin/python`,
    `./node_modules/.bin/vite`, `/usr/bin/make` — and those strings match none of
    the bare-name rules. The wildcard form was chosen by probing the real CLI
    (`tests/test_permission_patterns_live.py`), which is the only authority here:

        Bash(python *)            denied
        Bash(*python *)           denied      <- a bare leading * does NOT work
        Bash(*/python *)          ALLOWED     <- the wildcard must align to a `/`
        Bash(.venv/bin/python *)  ALLOWED     <- exact, but one path per rule

    So `*/` is the general form, and `*` alone is not. `Bash(*)` would also work
    and is deliberately not used: it grants every command, which is the ceiling
    this list exists to stay below.

    Non-Bash entries (`Read`, `Edit`, …) and argument-less ones (`Bash(env)`) are
    passed through untouched — there is no path to qualify.
    """
    out: list[str] = []
    for rule in rules:
        out.append(rule)
        if rule.startswith("Bash(") and rule != "Bash(env)":
            out.append(f"Bash(*/{rule[len('Bash(') :]}")
    return tuple(out)


#: The baseline as shipped: every rule above, plus its path-qualified twin.
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = _with_path_qualified_forms(_BASE_ALLOWED_TOOLS)


class EdgeWeightsConfig(BaseModel):
    """Affinity weights for the codegraph signals (plan R3) and the prose fallback.

    ``prose_neighbor`` is not a codegraph signal: it is the affinity a region-less
    task gets toward its plan-order neighbor so unmappable tasks cluster near the
    work they were written next to.

    ``semantic`` weights one matched task-map route-tag edge
    (``implements``/``consumes``, docs/orchestrator-task-map.md); the layer is then
    scaled by ``clamp(Σw_struct / Σw_sem, semantic_floor, semantic_ceil)`` so
    semantics dominate only when the structural layer is near-empty (greenfield)
    and never override real reference edges on edit-heavy plans.
    """

    shared_file: float = 1.0
    call: float = 2.0
    impact: float = 1.5
    prose_neighbor: float = 0.5
    semantic: float = 1.5
    semantic_floor: float = 0.5
    semantic_ceil: float = 3.0


class PartitionConfig(BaseModel):
    hub_threshold: float = 0.4  # CoCoder's live ROLE_THRESHOLD
    louvain_resolution: float = 1.0
    # R5/plan U6: a declared slice whose own summed work exceeds the budget cap
    # is a hard GrouperError by default; this (and --allow-oversized-slice,
    # exactly equivalent) keeps it whole as one flagged group instead.
    allow_oversized_slice: bool = False
    # A partition whose cycle repair left a group over the cap is degenerate: the
    # repair collapsed an SCC it could not re-split, so the "groups" are one blob.
    # Hard error by default (this used to be a flag nobody blocked on, so `group`
    # exited 0 with a single 3.8x-over-cap group); this accepts it instead.
    allow_degenerate_partition: bool = False
    # Plan U4: the granularity dial. "independent" enforces both merge_small_groups
    # guards and reproduces today's default partition byte-for-byte; "balanced"
    # drops chain_compatible but still rejects a merge that regresses the
    # simulated makespan; "monolithic" also drops the makespan check. The budget
    # cap, slice must-link and cycle checks stay hard at every level. CLI
    # `--granularity` wins over this when both are set.
    granularity: Literal["independent", "balanced", "monolithic"] = "independent"


class EstimatorConfig(BaseModel):
    """Token-budget estimator knobs (plan U3). Directional; tuned on real plans."""

    token_budget: int = 100_000
    bytes_per_token: float = 4.0
    slack_multiplier: float = 1.3
    # Measured on run r20260819-crashrec (4 groups, 9 sessions; full write-up in
    # docs/2026-08-20-estimator-underestimation-findings.md).
    #
    # Everything else in this class models READ cost — base head + spec + source
    # bytes — and that model is accurate: reviewers, which read the group's
    # material roughly once, landed at 0.90x-1.35x of estimate. Coders landed at
    # 1.56x-3.83x, because their context is read cost *plus* iteration, and
    # iteration dominates. Measured coder context tracked ~1,000 tokens per
    # assistant turn (964-1,140 across every session), and turn count is not
    # predictable from source bytes: g1 touched no new files at all and still
    # overshot 3.26x, so this is not a greenfield-authoring effect.
    #
    # This multiplier converts the read-cost estimate into a predicted CODER
    # peak context, which is the number that has to fit `token_budget`. Applied
    # to the group estimate only — never to a reviewer figure, which needs no
    # correction.
    #
    # Direction matters: raising this makes groups SMALLER and more numerous
    # (at 2.5 against a 200k budget the effective read-cost cap is ~80k), which
    # is the point — a group sized to fill 200k of *read* cost costs its coder
    # ~500k and gets retired mid-work, losing its warm context.
    #
    # 2.5 is deliberately below the 3.26x median: the breaker is a quality
    # guard, not a throughput limit (past ~200k the model degrades), so the
    # correct response to overshoot is smaller groups, not a higher breaker.
    coder_slack_multiplier: float = 2.5
    per_file_tool_allowance: int = 2_000
    spec_tokens_allowance: int = 3_000  # partition-time stand-in before specs exist
    # Plan U7: a prospective file with a declared size_hints class is priced here
    # instead of per_file_tool_allowance; medium equals today's flat rate by
    # design, so an unhinted prospective file is priced exactly as before.
    size_hint_small: int = 500
    size_hint_medium: int = 2_000
    size_hint_large: int = 5_000


class DifficultyConfig(BaseModel):
    """Difficulty = weighted sum of saturating-normalized signals, in [0, 1).

    Each signal x is normalized as x / (x + scale): the scale is the raw value at
    which that signal contributes half its weight. Tier thresholds pick the review
    intensity (origin R15): < d_review → self-verify, < d_hard → paired reviewer,
    else paired plus one mandatory extra round.
    """

    weight_files_touched: float = 1.0
    weight_max_fan: float = 1.5
    weight_hub_touches: float = 2.0
    weight_cross_group_edges: float = 1.5
    weight_verification_items: float = 1.0

    scale_files_touched: float = 6.0
    scale_max_fan: float = 10.0
    scale_hub_touches: float = 1.0
    scale_cross_group_edges: float = 3.0
    scale_verification_items: float = 5.0

    d_review: float = 0.35
    d_hard: float = 0.65


class BreakerConfig(BaseModel):
    """Circuit-breaker thresholds (origin R14; plan Key Technical Decisions).

    ``context_token_limit`` default matches measured reality (plan U7): the
    120k default retired healthy coders whose real occupancy was nowhere near
    it, once the RoundUsage fix (plan context-token P0) made the signal
    accurate.

    Raised to 250k on 2026-08-20 to sit just above the 200k sizing budget, giving
    a correctly sized group ~25% headroom while still catching one that is
    genuinely misbehaving. Deliberately NOT raised further to accommodate
    oversized groups: past roughly 200k of context the model degrades, so
    retiring a 300k coder is the breaker defending output quality, and the fix
    for chronic overshoot is smaller groups — see
    ``EstimatorConfig.coder_slack_multiplier`` and
    docs/2026-08-20-estimator-underestimation-findings.md.
    """

    context_token_limit: int = 250_000
    max_rounds_per_generation: int = 3
    max_generations: int = 3
    # Bound on the envelope side (plan U1 Decisions): a group re-entered this many
    # times after an unrecognised (INTERRUPTED) exception is quarantined rather
    # than re-entered again on the next resume — `retry` is what releases it.
    # Nothing bounded this before: a group could die under the harness and be
    # silently re-entered forever with no counter anywhere recording it.
    max_reentries: int = 3
    # Plan U3: staged in-round prompts at 70%/90%/100% of context_token_limit,
    # riding the per-turn observer the streaming channel (plan U1) provides —
    # bounds *cost* inside a round, not stuck-ness (that's R7's wall-clock
    # rejection; a token ceiling is a proxy for the former, never the latter).
    # Off by default so an existing run/test is unaffected until it opts in.
    context_ladder_enabled: bool = False


class ExecutionConfig(BaseModel):
    # Serial by default: each group's worktree is cut from the integration tip at
    # its ready→running transition, so one-at-a-time stacks each group on the
    # prior's merged work — no cross-group merge conflicts, and a usage-limit hit
    # costs at most one in-flight group. Raise via `--concurrency N` for throughput
    # when rate-limit pressure is low.
    concurrency: int = 1
    sequential: bool = False  # R25: deterministic one-at-a-time first-debug mode
    permission_mode: str = "acceptEdits"
    # Spec rewrites per group before it fails. The plan bounds respawns via the
    # generation cap but leaves the rewrite loop bound to implementation (U7).
    max_rewrites: int = 2
    # Warm-resume attempts at the group's own coder session to resolve a merge
    # conflict in place, before falling back to a full spec rewrite (plan U1).
    # Serial-by-default (concurrency=1) makes cross-group conflicts rare, so one
    # attempt from the session that just built the work is the right cost/benefit
    # ahead of the proven (but expensive) rewrite path.
    max_conflict_resolve_attempts: int = 1
    # What admission does once a group has ended unsuccessfully (plan U3/R41).
    # "halt": no further group is admitted once any group is FAILED or
    # INTERRUPTED — in-flight groups still run to their own terminal state, they
    # are just never joined by a new one forking from a tip that may carry a hole
    # or unverified resolve-merged work. "overlap" keeps the pre-U3 behaviour:
    # only groups whose declared files overlap the failed/interrupted group are
    # held.
    on_group_failure: Literal["halt", "overlap"] = "halt"
    # Reviewer scratch archive cap (plan U6): files beyond this many bytes are
    # left out of the archive (and named, with their size, in skipped.txt)
    # rather than silently dropped or grown without bound.
    review_scratch_cap_bytes: int = 100_000_000


class PreflightConfig(BaseModel):
    """The mechanical, LLM-free merge gate (plan U4).

    ``check_command`` is resolved once per merge attempt: the configured value
    if set, else detected from the worktree's own markers
    (``preflight.detect_check_command``); ``None`` when neither applies means
    no check command is run at all — Preflight still enforces the clean-tree
    check alone.
    """

    check_command: list[str] | None = None
    # A hung check command holds IntegrationMerger's lock and stalls every
    # other group's merge — the same silent-stall class this work closes.
    # A timeout is therefore always a failure, never a degrade to "no check
    # applied" (plan Decisions).
    check_timeout_s: float = 900.0


class UsageLimitConfig(BaseModel):
    """What a run does when the account's usage limit is reached.

    The default is to wait it out. Before this existed a limit ended the run —
    the reset time the classifier's regex had just matched was discarded, the
    scheduler marked the group INTERRUPTED, and a human had to notice and type
    `resume`. One recorded run spent most of ~2.7 days that way.

    ``max_wait_s = 0`` means "however long it takes", weekly limits included:
    the pause costs nothing but wall clock, and the alternative is a stopped run
    nobody is watching. Set it to bound the wait when a run has a deadline.
    """

    auto_resume: bool = True
    max_wait_s: float = 0.0
    # Retries of the *same* call, across pauses, before the limit is treated as
    # unrecoverable and today's INTERRUPTED path takes over unchanged.
    max_attempts: int = 6
    # Retry this far *after* the announced reset. The reset time is a claim, not
    # a guarantee, and a retry that lands one second early spends an attempt.
    skew_s: float = 60.0
    # Only used when the prose carries no parseable reset time: re-check on this
    # interval rather than guessing a deadline.
    fallback_poll_s: float = 900.0


class SessionConfig(BaseModel):
    """How the run command shells the claude CLI (plan U9).

    ``claude_bin`` accepts a list so tests point it at the stub interpreter
    (``["python", "tests/fake_claude.py"]``); ``transcript_root`` overrides the
    ``~/.claude/projects`` default for the same reason.
    """

    claude_bin: str | list[str] = "claude"
    model: str | None = None
    # What a worker is permitted to execute, declared by the *run* rather than
    # inherited from whoever launched it.
    #
    # This shipped empty, so the flag was never passed and a worker could only run
    # commands enumerated in the operator's personal `~/.claude/settings.json`
    # (workers run headless under `acceptEdits`, so anything unlisted is denied
    # outright with no approver to ask). On run r20260812-202855 that operator had
    # `Bash(git *)` and `Bash(uv *)` but no npm rule, so g8 wrote its whole client
    # and then failed three rounds running on `npm install --prefix web` — a
    # failure indistinguishable, from the outside, from confinement being too
    # tight. It cost this validation an incorrect diagnosis before the argv was
    # checked.
    #
    # The baseline is the toolchain a coder must drive to build and verify work.
    # It is deliberately not "everything": the denied git mutators and the
    # operator-memory rules in `disallowed_tools` still apply on top, and deny
    # beats allow.
    allowed_tools: list[str] = Field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))
    transcript_root: str | None = None
    # Thinking budget per worker turn. Left unset the CLI picks its own default,
    # which is neither pinned nor visible in any run artifact — and thinking counts
    # as *output* tokens, so it lands squarely on the cost driver measured on run
    # r20260729-correctness (588k output tokens across 342 turns in one round).
    # The CLI's level names map to budgets by the documented convention:
    # medium 4000 / high 10000 / xhigh 31999. Default to medium; raise per-run in
    # config.toml when a group genuinely needs deeper reasoning.
    # NB: `--max-thinking-tokens` is hidden from `claude --help`, so it must never
    # go in REQUIRED_CLI_FLAGS or preflight would reject every real CLI.
    max_thinking_tokens: int | None = 4000
    # Orthogonal to the budget above: `--thinking` gates *whether* a turn thinks
    # (enabled = always, adaptive = the model decides, disabled = never), while
    # max_thinking_tokens caps how far it may go when it does. Measured on one probe
    # (sonnet, same prompt): adaptive 62 output tokens vs enabled 140 vs disabled 253
    # — adaptive was both cheapest and correct, so it is the default. Pairing it with
    # the medium ceiling means "think only when it helps, never more than medium".
    thinking: str | None = "adaptive"
    # Plan U2: kernel-enforced confinement via Landlock, layered under the
    # deny-rules below rather than instead of them (deny-rules give a clearer
    # error for the accidental case; Landlock is the actual boundary).
    #
    # On by default. It shipped off, on the reasoning that a run should opt in —
    # but the CLI then never passed it at all, so the whole mechanism sat dead
    # for a release while the P0 it closes (workers editing the operator's
    # auto-memory) stayed open. An opt-in boundary that nothing opts into is not
    # a boundary. Defaulting on is safe because absence degrades to a warning
    # and deny-rules rather than failing a group.
    confine: bool = True
    # --disallowedTools patterns (e.g. the denied git subcommands from
    # worktrees.denied_git_tool_patterns()) and an optional --settings path or
    # inline JSON string. Empty/None means the flag is omitted entirely.
    disallowed_tools: list[str] = Field(default_factory=list)
    settings: str | None = None
    # Where every worker's toolchain caches go, shared across groups *and* runs.
    # Defaults to `${XDG_CACHE_HOME:-$HOME/.cache}/smart-mcps-orchestrator`. The
    # override exists chiefly for the cross-filesystem case: `uv` finishes a venv
    # by renaming out of its cache, and a cache on a different filesystem from the
    # repo makes that rename fail with EXDEV. Point this at the repo's filesystem
    # when that happens.
    cache_root: str | None = None
    # The escape hatch that makes "stop enumerating" true rather than aspirational.
    #
    # Redirecting caches by environment covers every tool that honours its own
    # cache variable. Some do not — `~/.bun`, `~/.nuget`, `~/.gem`, `~/.ivy2`,
    # `~/.pub-cache`, `~/.deno` hardcode a home path — and each of those used to
    # mean a new line in the confinement source and a new release. Here they are
    # one config line an operator writes for their own project.
    extra_write_paths: list[str] = Field(default_factory=list)
    # Arguments appended to the `uv sync` that provisions a group's worktree venv.
    # `--all-extras` by default: a group's venv should mirror the dev environment
    # it is verified against, or its reviewer cannot tell a missing extra from a
    # regression. It can be heavy on projects with large optional extras — set
    # this to `[]` to opt out.
    provision_args: list[str] = Field(default_factory=lambda: ["--all-extras"])
    # What to do when the account's usage limit is reached mid-run: by default,
    # pause in place and retry the identical call once the limit releases.
    usage_limit: UsageLimitConfig = Field(default_factory=UsageLimitConfig)


class EscalationConfig(BaseModel):
    """Human-in-the-loop escalation surface (plan Phase D).

    **HITL is opt-in.** ``enabled`` is off and ``intensity`` is ``autonomous``,
    so a plain ``run`` never pauses for an operator; ``--hitl`` (or
    ``[escalation] enabled = true``) turns it on.

    This default was once ``enabled=True, intensity="on_stuck"`` (plan U2), on
    the grounds that a group ending failed or interrupted must never let an
    overlapping successor start silently, and that gate needs an operator
    channel. That rationale is stale: U3 shipped
    ``ExecutionConfig.on_group_failure``, defaulting to ``"halt"``, which stops
    admission on a failure mechanically, with no operator involved. The safety
    case HITL-on was defending is now covered without blocking an unattended run.

    When enabled, the ``intensity`` tier decides which hard moments pause for
    the operator (``autonomous`` < ``on_failure`` < ``on_stuck`` <
    ``interactive``) and ``source`` decides whether a coder's ``needs_input``
    question reaches the operator (``workers_via_orchestrator``) or is
    downgraded to a blocked-style rewrite (``orchestrator_only``). Note that
    ``intensity = "autonomous"`` forces ``enabled`` off, which is why the two
    defaults agree rather than leaving an enabled-but-never-escalating state.

    ``timeout_s = None`` blocks indefinitely (a live operator is expected once
    HITL is on); when set, an unanswered escalation falls back per ``on_timeout``.
    """

    enabled: bool = False
    intensity: Literal["autonomous", "on_failure", "on_stuck", "interactive"] = "autonomous"
    source: Literal["orchestrator_only", "workers_via_orchestrator"] = "workers_via_orchestrator"
    timeout_s: float | None = None
    on_timeout: Literal["autonomous", "skip", "abort"] = "autonomous"
    poll_interval_s: float = 1.0


class OrchestratorConfig(BaseModel):
    edge_weights: EdgeWeightsConfig = Field(default_factory=EdgeWeightsConfig)
    partition: PartitionConfig = Field(default_factory=PartitionConfig)
    estimator: EstimatorConfig = Field(default_factory=EstimatorConfig)
    difficulty: DifficultyConfig = Field(default_factory=DifficultyConfig)
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    preflight: PreflightConfig = Field(default_factory=PreflightConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)


def load_config(path: Path | None = None) -> OrchestratorConfig:
    """Load config from a TOML file; every field falls back to its default.

    ``path=None`` or a missing default file yields pure defaults. U9 resolves the
    conventional location (``.orchestrator/config.toml`` in the target repo).
    """
    if path is None or not path.is_file():
        return OrchestratorConfig()
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    # Raw-TOML detection (R7): pydantic v2 silently ignores unknown keys, so a
    # config still carrying the removed per-round timeout would be dropped
    # without a trace — warn explicitly before validation.
    session = data.get("session")
    if isinstance(session, dict) and "timeout_s" in session:
        print(
            f"warning: {path}: [session] timeout_s is deprecated and ignored — "
            "the per-round timeout was removed (R7)",
            file=sys.stderr,
        )
    return OrchestratorConfig.model_validate(data)
