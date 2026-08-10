// One `GroupState` → colour/label map, and the rules for reading a group's
// status honestly.
//
// This is the single place a state becomes a colour. `Record<GroupState, …>`
// means adding a state to the union and forgetting it here fails the TypeScript
// build rather than rendering an unstyled badge — which is how `resolved` and
// `interrupted` shipped invisible once already. A second, runtime guard covers
// what types cannot: the API is a separate deployable and can send a state this
// bundle has never heard of.
//
// Two rules are encoded here rather than left to each component:
//
// **Amber means "needs the operator's attention", and nothing else.** An
// escalation is solid amber; an inferred stall is hatched amber. The four busy
// states (`running` / `reviewing` / `rewriting` / `merging`) collapse to one
// blue hue and are told apart by glyph and label, not by four near-identical
// hues nobody can distinguish on a board.
//
// **"Stalled" is an inference and the UI says so.** There is no `GroupState`
// for it and none should be added. It is computed, never persisted — persisting
// it would create a de facto state that later code branches on — and it renders
// as an overlay with a `?`, never as a state colour. It must never consult
// `live_pids`: those are display-only, and a run whose orchestrator crashed has
// to render exactly like a finished one.

import type { GroupState, SnapshotGroup } from "./types";

export interface StatusStyle {
  /** CSS custom-property value; one hue per *meaning*, not per state. */
  colour: string;
  label: string;
  /** Distinguishes states that share a hue. */
  glyph: string;
  /** Dashed border: not finished, but nothing went wrong. */
  dashed?: boolean;
}

const BLUE = "var(--status-busy, #2f6fb0)";
const GREY = "var(--status-idle, #8a8f98)";
const GREEN = "var(--status-done, #2e7d54)";
const RED = "var(--status-failed, #b3403a)";
const SLATE = "var(--status-interrupted, #5b6472)";
const MUTED_GREEN = "var(--status-resolved, #5d8f74)";

export const STATUS: Record<GroupState, StatusStyle> = {
  pending: { colour: GREY, label: "pending", glyph: "·" },
  ready: { colour: GREY, label: "ready", glyph: "▹" },
  running: { colour: BLUE, label: "running", glyph: "▶" },
  reviewing: { colour: BLUE, label: "reviewing", glyph: "◉" },
  rewriting: { colour: BLUE, label: "rewriting", glyph: "✎" },
  merging: { colour: BLUE, label: "merging", glyph: "⑂" },
  completed: { colour: GREEN, label: "completed", glyph: "✓" },
  // A FAILED group whose stranded work the operator landed. A muted success,
  // not a second failure colour: the work is not lost.
  resolved: { colour: MUTED_GREEN, label: "resolved", glyph: "✓" },
  failed: { colour: RED, label: "failed", glyph: "✕", dashed: true },
  // The harness died under the group, not the work. Dashed like `failed` to
  // signal "not finished", but slate rather than red, because nothing went
  // wrong — it is resumable with a plain `resume`.
  interrupted: { colour: SLATE, label: "interrupted", glyph: "⏸", dashed: true },
};

// The compile-time guard is `Record<GroupState, StatusStyle>` above: adding a
// member to the union without adding a style here fails `tsc`, which is exactly
// what did not happen when `resolved` and `interrupted` shipped invisible.
//
// This is the second, independent guard, for the case types cannot cover: the
// API is a separate deployable, so a running backend can send a state string
// this bundle has never heard of. That renders as a visibly unknown badge —
// never as a blank one, and never as a crash.
export const UNKNOWN_STATUS: StatusStyle = {
  colour: "var(--status-unknown, #7a5ea8)",
  label: "unknown state",
  glyph: "?",
  dashed: true,
};

export function statusOf(state: GroupState | string): StatusStyle {
  return STATUS[state as GroupState] ?? UNKNOWN_STATUS;
}

/**
 * A generation the group has already moved past.
 *
 * Not a `GroupState` — `state.json` only ever describes the latest attempt, so
 * an earlier generation has no state of its own and inventing one would be a
 * claim the data does not support. It lives here rather than in the attempt
 * grid so that the grid keeps having no colour logic of its own: this file
 * stays the single place anything becomes a colour.
 */
export const SUPERSEDED_STATUS: StatusStyle = {
  colour: "var(--status-superseded, #8a8f98)",
  label: "superseded",
  glyph: "↺",
  dashed: true,
};

/**
 * Amber, and only for "needs the operator's attention".
 *
 * Solid for an escalation — the operator is being asked something — and hatched
 * (via CSS) for an inferred stall. Deliberately not a member of `STATUS`:
 * escalation-blocked is orthogonal to state, a group can be blocked in any of
 * the busy states, and folding it into the state colour would lose which.
 */
export const ATTENTION_COLOUR = "var(--status-attention, #c98a1b)";

/** Call sites that switch on a state use this to stay exhaustive. */
export function assertNever(value: never): never {
  throw new Error(`unhandled GroupState: ${String(value)}`);
}

/** States in which a group holds a live worktree it may still be writing to. */
export const ACTIVE_STATES: readonly GroupState[] = [
  "running",
  "reviewing",
  "rewriting",
  "merging",
];

export const STALL_THRESHOLD_MS = 15 * 60 * 1000;

export interface StallInference {
  stalled: boolean;
  /** A fact — "no activity for 23m" — never a claim like "hung". */
  note: string;
}

/**
 * Whether a group *looks* stalled, stated as an observation.
 *
 * `state ∈ ACTIVE_STATES && no run-dir writes for > 15min && no pending
 * escalation`. A group waiting on the operator is not stalled, it is blocked,
 * and those are different things with different next actions.
 *
 * Note what is absent: `live_pids`. A pid is not consulted here and must not be
 * — see the module header.
 */
export function inferStall(
  group: SnapshotGroup,
  lastWriteMs: number | null,
  hasPendingEscalation: boolean,
  nowMs: number = Date.now(),
): StallInference {
  if (!ACTIVE_STATES.includes(group.state)) return { stalled: false, note: "" };
  if (hasPendingEscalation) return { stalled: false, note: "waiting on the operator" };
  if (lastWriteMs === null) return { stalled: false, note: "" };

  const idle = nowMs - lastWriteMs;
  if (idle <= STALL_THRESHOLD_MS) return { stalled: false, note: "" };
  return { stalled: true, note: `no activity for ${formatDuration(idle)}` };
}

export function formatDuration(ms: number): string {
  const minutes = Math.floor(ms / 60000);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

/**
 * Whether a group's `failure` text describes its current state.
 *
 * `GroupRunState` is single-valued and last-writer-wins: it cannot say "failed
 * once, then succeeded", so a resolved group keeps the old failure string. The
 * backend flags that as `stale_failure`; this is the one place the UI decides
 * what to do about it, and the answer is "show it as history, never as a
 * failure".
 */
export function failureIsCurrent(group: SnapshotGroup): boolean {
  return Boolean(group.failure) && !group.stale_failure;
}
