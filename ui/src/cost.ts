// Cost accounting, derived. Two quantities, kept apart on purpose.
//
// **The one thing this file exists to protect.** The grouper's
// `estimated_tokens` predicts *context occupancy for a single coder session*.
// The four cumulative counters on a session are *spend, summed over every round
// and every role*. They are different quantities in different units of meaning,
// and dividing one by the other produces a number that looks like a calibration
// error and is not one. So:
//
//   - `groupPrediction` compares `estimated_tokens` against the coder session's
//     `last_context_tokens` — the same quantity, prediction vs outcome — and is
//     the only place in this module that computes a ratio at all.
//   - `groupSpend` sums the token classes and never sees `estimated_tokens`.
//     It cannot form a ratio against the estimate because it is not given one.
//
// **Cumulative figures are sums of per-round values.** When a session carries a
// `rounds` list, the totals here are built by adding those rounds up. When it
// does not, the session's cumulative counters are used — and those are
// themselves per-round sums written by `SessionUsage.add`. Nothing here reads
// `last_context_tokens` into a cumulative figure: that is the latest round's
// occupancy, and treating it as spend is the same class of mistake as reading
// the envelope's top-level `usage`, which summed every turn and produced the
// 50x-inflated context figure that retired healthy coders.
//
// Everything is a pure function of the snapshot; the component renders what it
// returns and decides nothing.

import { hasActuals } from "./attempts";
import type { ReviewIntensity, RunSnapshot, SnapshotGroup, SnapshotSession } from "./types";

// ------------------------------------------------------------- token classes

export type TokenClassKey = "uncached_input" | "cache_creation" | "cache_read" | "output";

export interface TokenClasses {
  uncached_input: number;
  cache_creation: number;
  cache_read: number;
  output: number;
}

export interface TokenClassStyle {
  key: TokenClassKey;
  label: string;
  /**
   * `muted` classes are drawn subordinate — lower contrast, no border, last in
   * the legend. Cache reads are the only one, and deliberately so: they are the
   * cheap class, a session whose spend is mostly cache-read is *healthy*, and a
   * bar that gives them the loudest segment tells the operator the opposite of
   * the truth.
   */
  emphasis: "primary" | "muted";
  colour: string;
  hint: string;
}

export const TOKEN_CLASSES: readonly TokenClassStyle[] = [
  {
    key: "uncached_input",
    label: "uncached input",
    emphasis: "primary",
    colour: "var(--cost-uncached-input)",
    hint: "Input tokens that were not served from cache — the expensive input class.",
  },
  {
    key: "cache_creation",
    label: "cache written",
    emphasis: "primary",
    colour: "var(--cost-cache-creation)",
    hint: "Input tokens written into the prompt cache; paid once, read cheaply after.",
  },
  {
    key: "output",
    label: "output",
    emphasis: "primary",
    colour: "var(--cost-output)",
    hint: "Tokens the model generated.",
  },
  {
    key: "cache_read",
    label: "cache read",
    emphasis: "muted",
    colour: "var(--cost-cache-read)",
    hint: "Tokens served from the prompt cache — the cheap class. A bar that is mostly cache read is a healthy session, not an expensive one.",
  },
];

export function emptyClasses(): TokenClasses {
  return { uncached_input: 0, cache_creation: 0, cache_read: 0, output: 0 };
}

export function addClasses(a: TokenClasses, b: TokenClasses): TokenClasses {
  return {
    uncached_input: a.uncached_input + b.uncached_input,
    cache_creation: a.cache_creation + b.cache_creation,
    cache_read: a.cache_read + b.cache_read,
    output: a.output + b.output,
  };
}

export function totalOf(classes: TokenClasses): number {
  return classes.uncached_input + classes.cache_creation + classes.cache_read + classes.output;
}

/** The classes the operator is asked to read as cost — cache reads excluded,
 * because they are the class this panel exists to de-emphasise. */
export function billableish(classes: TokenClasses): number {
  return classes.uncached_input + classes.cache_creation + classes.output;
}

// ------------------------------------------------------------------ sessions

export type CostRole = "coder" | "reviewer" | "base";

const ROLE_ORDER: readonly CostRole[] = ["coder", "reviewer", "base"];

/**
 * One session's cumulative spend, and where the number came from.
 *
 * `per_round` means the session carried a `rounds` list and the totals below
 * are that list added up. `cumulative_counters` means it did not, so the
 * session's own counters were used — which `SessionUsage.add` builds by summing
 * the same per-round values on the way in. Either way the figure is a sum of
 * rounds; neither path reads a session-level envelope total.
 */
export type SpendSource = "per_round" | "cumulative_counters" | "none";

export interface SessionCost {
  sessionId: string;
  name: string;
  role: CostRole;
  generation: number;
  classes: TokenClasses;
  total: number;
  source: SpendSource;
  /** Per-round totals, oldest first. Empty unless the manifest carried rounds. */
  roundTotals: number[];
  roundsCompleted: number;
  /** Latest round's context occupancy. Never added into a cumulative figure. */
  lastContextTokens: number;
  /** Sum of every round's turn-1 inherited cache read (plan U9) — context this
   * session did not create and cannot shrink. Its own figure, distinct from
   * `classes.cache_read`, which is every turn's cache read summed. */
  inheritedCacheReadTokens: number;
  model: string | null;
  retirementReason: string | null;
}

function roleOf(session: SnapshotSession): CostRole {
  return session.role === "coder" || session.role === "reviewer" || session.role === "base"
    ? session.role
    : "coder";
}

/** True once anything about this session's actuals was recorded. Runs written
 * before the token-class split have all four counters at 0 *and* no timing, and
 * that is a different claim from "this session spent nothing". */
export function sessionHasCost(session: SnapshotSession): boolean {
  return (
    hasActuals(session) ||
    (session.rounds?.length ?? 0) > 0 ||
    session.total_input_tokens > 0 ||
    session.total_output_tokens > 0 ||
    session.total_cache_read_tokens > 0 ||
    session.total_cache_creation_tokens > 0
  );
}

export function sessionCost(session: SnapshotSession): SessionCost {
  const rounds = session.rounds ?? [];
  let classes: TokenClasses;
  let source: SpendSource;

  if (rounds.length > 0) {
    // Summed round by round — the whole point. The session-level envelope
    // total is never consulted, here or anywhere downstream.
    classes = rounds.reduce(
      (acc, round) =>
        addClasses(acc, {
          uncached_input: round.input_tokens,
          cache_creation: round.cache_creation_input_tokens,
          cache_read: round.cache_read_input_tokens,
          output: round.output_tokens,
        }),
      emptyClasses(),
    );
    source = "per_round";
  } else {
    classes = {
      uncached_input: session.total_input_tokens,
      cache_creation: session.total_cache_creation_tokens,
      cache_read: session.total_cache_read_tokens,
      output: session.total_output_tokens,
    };
    source = sessionHasCost(session) ? "cumulative_counters" : "none";
  }

  return {
    sessionId: session.session_id,
    name: session.name,
    role: roleOf(session),
    generation: session.generation,
    classes,
    total: totalOf(classes),
    source,
    roundTotals: rounds.map(
      (r) =>
        r.input_tokens +
        r.output_tokens +
        r.cache_read_input_tokens +
        r.cache_creation_input_tokens,
    ),
    roundsCompleted: session.rounds_completed,
    lastContextTokens: session.last_context_tokens,
    inheritedCacheReadTokens: session.total_inherited_cache_read_tokens ?? 0,
    model: session.model ?? null,
    retirementReason: session.retirement_reason ?? null,
  };
}

// --------------------------------------------------------------------- roles

export interface RoleCost {
  role: CostRole;
  sessions: SessionCost[];
  classes: TokenClasses;
  total: number;
}

// -------------------------------------------------------------- expectations

/**
 * How many reviewer sessions this group's `intensity` calls for.
 *
 * Read off the review loop rather than guessed: `self_verify` creates no
 * reviewer session at all (`review.py:435`), while `paired` and `paired_plus`
 * both create exactly one and resume it — `paired_plus` differs by one
 * mandatory extra verification *round* inside that session (`review.py:474`),
 * not by a second session.
 *
 * `known: false` is its own answer. Older `groups.json` files may not carry
 * `intensity`, and a group with no reviewer sessions and no known intensity is
 * unexplained data, not confirmed-correct data. Saying "unknown" is the honest
 * rendering; assuming a default would silently turn a missing reviewer into an
 * expected one.
 */
export interface ReviewerExpectation {
  known: boolean;
  intensity: ReviewIntensity | null;
  expectedSessions: number | null;
  observedSessions: number;
  /** True when the observation matches what the intensity calls for. */
  asExpected: boolean;
  text: string;
}

export function reviewerExpectation(
  intensity: ReviewIntensity | null | undefined,
  observedSessions: number,
): ReviewerExpectation {
  if (intensity == null) {
    return {
      known: false,
      intensity: null,
      expectedSessions: null,
      observedSessions,
      asExpected: false,
      text: `intensity unknown — ${observedSessions} reviewer ${plural(observedSessions, "session")} observed, and no recorded intensity to say how many were expected`,
    };
  }
  const expected = intensity === "self_verify" ? 0 : 1;
  const extra = intensity === "paired_plus" ? ", plus one mandatory extra round" : "";
  const asExpected = observedSessions === expected;
  const base = `${intensity}: ${expected} reviewer ${plural(expected, "session")} expected${extra}`;
  let text: string;
  if (asExpected && expected === 0) {
    text = `${base} — zero reviewer sessions is correct for this group, not missing data`;
  } else if (asExpected) {
    text = `${base} — ${observedSessions} observed`;
  } else {
    text = `${base} — ${observedSessions} observed`;
  }
  return { known: true, intensity, expectedSessions: expected, observedSessions, asExpected, text };
}

function plural(n: number, word: string): string {
  return n === 1 ? word : `${word}s`;
}

// ---------------------------------------------------------------- prediction

/**
 * Prediction vs outcome, and nothing else.
 *
 * Both sides are context occupancy of one coder session: `estimated_tokens` is
 * what the estimator predicted it would be, `last_context_tokens` is what it
 * turned out to be. That is the only comparison in this codebase where a ratio
 * means anything, which is why `ratio` lives here and takes no other input.
 *
 * When a group ran more than one coder generation, the *latest* coder session
 * with a recorded occupancy is the outcome — the estimate described one coder's
 * context, and adding two generations' occupancies together would rebuild
 * exactly the cross-quantity sum this module refuses to compute.
 */
export interface GroupPrediction {
  estimated: number | null;
  observed: number | null;
  observedSessionId: string | null;
  observedGeneration: number | null;
  /** observed / estimated. Null unless both sides are present and non-zero. */
  ratio: number | null;
  /** Peak context occupancy across every coder generation, not just the last. A
   * multi-generation group can have burned its worst occupancy on a generation
   * that was later retired — the last-generation ratio alone would flatter it. */
  peak: number | null;
  peakSessionId: string | null;
  peakGeneration: number | null;
  peakRatio: number | null;
  /** Number of coder generations with a recorded occupancy. */
  generations: number;
  /** True once a group has burned more than one coder generation — its median
   * is computed over single-generation rows only, so this flags exclusion. */
  multiGeneration: boolean;
  /** Why each earlier (non-latest) generation was retired, oldest first. */
  retirementReasons: string[];
  comparable: boolean;
  note: string;
}

function groupPrediction(group: SnapshotGroup, sessions: SessionCost[]): GroupPrediction {
  const estimated = group.estimated_tokens ?? null;
  const coders = sessions.filter((s) => s.role === "coder" && s.lastContextTokens > 0);
  const latest = coders.length > 0 ? coders[coders.length - 1] : null;
  const observed = latest?.lastContextTokens ?? null;
  const peakSession = coders.reduce<SessionCost | null>(
    (best, s) => (best == null || s.lastContextTokens > best.lastContextTokens ? s : best),
    null,
  );
  const peak = peakSession?.lastContextTokens ?? null;
  const comparable = estimated != null && estimated > 0 && observed != null && observed > 0;
  const peakComparable = estimated != null && estimated > 0 && peak != null && peak > 0;
  const generations = coders.length;
  const retirementReasons = coders
    .filter((s) => s !== latest && s.retirementReason)
    .map((s) => s.retirementReason as string);
  return {
    estimated,
    observed,
    observedSessionId: latest?.sessionId ?? null,
    observedGeneration: latest?.generation ?? null,
    ratio: comparable ? (observed as number) / (estimated as number) : null,
    peak,
    peakSessionId: peakSession?.sessionId ?? null,
    peakGeneration: peakSession?.generation ?? null,
    peakRatio: peakComparable ? (peak as number) / (estimated as number) : null,
    generations,
    multiGeneration: generations > 1,
    retirementReasons,
    comparable,
    note: comparable
      ? "predicted vs observed context occupancy of one coder session — the same quantity on both sides"
      : estimated == null
        ? "no estimate recorded for this group"
        : "no coder context occupancy recorded for this group",
  };
}

// --------------------------------------------------------------------- group

export interface GroupCost {
  groupId: string;
  name: string;
  intensity: ReviewIntensity | null;
  difficulty: number | null;
  prediction: GroupPrediction;
  expectation: ReviewerExpectation;
  roles: RoleCost[];
  classes: TokenClasses;
  total: number;
  /** False when this group's manifest predates per-session actuals. */
  actualsRecorded: boolean;
  sessions: SessionCost[];
}

export function groupCost(group: SnapshotGroup): GroupCost {
  const sessions = group.sessions.map(sessionCost);
  const actualsRecorded = group.sessions.some(sessionHasCost);

  const roles: RoleCost[] = [];
  for (const role of ROLE_ORDER) {
    const members = sessions.filter((s) => s.role === role);
    if (members.length === 0 && role === "base") continue;
    const classes = members.reduce((acc, s) => addClasses(acc, s.classes), emptyClasses());
    roles.push({ role, sessions: members, classes, total: totalOf(classes) });
  }

  const classes = roles.reduce((acc, r) => addClasses(acc, r.classes), emptyClasses());
  const reviewerSessions = sessions.filter((s) => s.role === "reviewer").length;

  return {
    groupId: group.group_id,
    name: group.name,
    intensity: group.intensity ?? null,
    difficulty: group.difficulty ?? null,
    prediction: groupPrediction(group, sessions),
    expectation: reviewerExpectation(group.intensity, reviewerSessions),
    roles,
    classes,
    total: totalOf(classes),
    actualsRecorded,
    sessions,
  };
}

// --------------------------------------------------------------- calibration

/**
 * Estimator calibration across the run: how far `bytes_per_token` and
 * `slack_multiplier` are off.
 *
 * Every row is a same-quantity pair, so the summary ratio is a ratio of like
 * for like. Groups with no estimate or no recorded occupancy are counted as
 * skipped and named — a calibration figure computed over an unstated subset is
 * how a tuned-looking number gets reported for an untuned estimator.
 */
export interface CalibrationRow {
  groupId: string;
  name: string;
  estimated: number;
  /** Last-generation occupancy — what the group's final coder generation saw. */
  observedLast: number;
  /** Peak occupancy across every coder generation. Equal to `observedLast` for
   * a single-generation row. */
  observedPeak: number;
  ratioLast: number;
  ratioPeak: number;
  /** Number of coder generations with a recorded occupancy. */
  generations: number;
  /** True once this row has more than one generation — excluded from the
   * median and the aggregate, which are meaningless over a mixed population. */
  multiGeneration: boolean;
  /** Why each earlier generation was retired, oldest first; empty for a
   * single-generation row. */
  retirementReasons: string[];
}

export interface RunCalibration {
  /** Every comparable group, including multi-generation ones — hiding a row is
   * worse than labelling it, since this panel exists for manual reading. */
  rows: CalibrationRow[];
  /** Groups with no estimate or no recorded coder occupancy at all. */
  skipped: string[];
  /** Sums are over single-generation rows only. */
  estimatedTotal: number;
  observedTotal: number;
  /** Median of the single-generation rows' last-generation ratios; robust to
   * the one runaway session. Null when there are none to compute it from. */
  medianRatio: number | null;
  /** Ratio of the summed occupancies — both sides the same quantity, over
   * single-generation rows only. */
  aggregateRatio: number | null;
  /** How many of `rows` were single-generation vs excluded as multi-generation
   * — the population the summary states it was computed over. */
  singleGenerationCount: number;
  multiGenerationCount: number;
}

export function runCalibration(groups: GroupCost[]): RunCalibration {
  const rows: CalibrationRow[] = [];
  const skipped: string[] = [];
  for (const group of groups) {
    const p = group.prediction;
    if (!p.comparable || p.estimated == null || p.observed == null || p.ratio == null) {
      skipped.push(group.groupId);
      continue;
    }
    rows.push({
      groupId: group.groupId,
      name: group.name,
      estimated: p.estimated,
      observedLast: p.observed,
      observedPeak: p.peak ?? p.observed,
      ratioLast: p.ratio,
      ratioPeak: p.peakRatio ?? p.ratio,
      generations: p.generations,
      multiGeneration: p.multiGeneration,
      retirementReasons: p.retirementReasons,
    });
  }
  const singleGen = rows.filter((r) => !r.multiGeneration);
  const estimatedTotal = singleGen.reduce((a, r) => a + r.estimated, 0);
  const observedTotal = singleGen.reduce((a, r) => a + r.observedLast, 0);
  return {
    rows,
    skipped,
    estimatedTotal,
    observedTotal,
    medianRatio: median(singleGen.map((r) => r.ratioLast)),
    aggregateRatio: estimatedTotal > 0 ? observedTotal / estimatedTotal : null,
    singleGenerationCount: singleGen.length,
    multiGenerationCount: rows.length - singleGen.length,
  };
}

function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

// ----------------------------------------------------------------- run view

export interface CostView {
  runId: string;
  groups: GroupCost[];
  calibration: RunCalibration;
  /** False when no group in the run recorded any actuals — the legacy case. */
  actualsRecorded: boolean;
  classes: TokenClasses;
  total: number;
}

export function buildCostView(snapshot: RunSnapshot): CostView {
  const groups = snapshot.groups.map(groupCost);
  const classes = groups.reduce((acc, g) => addClasses(acc, g.classes), emptyClasses());
  return {
    runId: snapshot.run_id,
    groups,
    calibration: runCalibration(groups),
    actualsRecorded: groups.some((g) => g.actualsRecorded),
    classes,
    total: totalOf(classes),
  };
}

// ------------------------------------------------------------------ display

export function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 10_000 ? 0 : 1)}k`;
  return `${value}`;
}

/** "1.4x over" / "0.6x under" / "on the estimate" — the ratio in words, so the
 * bare number never has to carry the direction on its own. */
export function describeRatio(ratio: number | null): string {
  if (ratio == null) return "not comparable";
  if (ratio >= 1.1) return `${ratio.toFixed(2)}x over the estimate`;
  if (ratio <= 0.9) return `${ratio.toFixed(2)}x — under the estimate`;
  return `${ratio.toFixed(2)}x — on the estimate`;
}
