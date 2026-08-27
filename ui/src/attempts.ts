// The attempt history, derived. Rows are groups, columns are generations —
// the Airflow Grid shape, chosen over LangSmith's implicit nested retry rows so
// that "this group was attempted three times" is one glance and not an
// expand-every-row exercise.
//
// **`manifest.json` is the ground truth for what attempts existed.** Its
// per-group `sessions` list is append-only, so every attempt that ever ran is
// still in it. `state.json`'s `GroupRunState` is single-valued and
// last-writer-wins: it can say what the group is doing *now* and nothing else —
// it structurally cannot represent "failed once, then succeeded". So the
// columns of this grid come from the sessions, never from the state's
// `generation`, and the only thing the state contributes is the colour of the
// current column.
//
// Everything here is a pure function of the snapshot. No colour is chosen in
// this file either: `status.ts` remains the one place a status becomes a
// colour, and the cells carry its `StatusStyle` through unchanged.

import { SUPERSEDED_STATUS, inferStall, statusOf } from "./status";
import type { StatusStyle } from "./status";
import type { EscalationRequest, RunSnapshot, SnapshotGroup, SnapshotSession } from "./types";

/**
 * The seven special cases the plan names, plus the two honest-degradation
 * notes. Each is a *fact about the attempt*, rendered as a chip on the cell —
 * none of them is a state, and none of them changes the cell's colour.
 */
export type AttemptNoteKind =
  | "breaker_retirement"
  | "usage_limit_outage"
  | "escalation_blocked"
  | "superseded_by_respawn"
  | "self_verify_no_reviewer"
  | "interrupted_resumable"
  | "bookkeeping_lost"
  | "stale_failure"
  | "stalled"
  | "actuals_missing";

export interface AttemptNote {
  kind: AttemptNoteKind;
  /** One line, stating a fact. Never a diagnosis. */
  text: string;
  /** Longer explanation for the title attribute / expanded view. */
  detail?: string;
  /** Shown as copyable text — never wired to a button. See `resumeCommand`. */
  copyable?: string;
}

export interface AttemptCell {
  groupId: string;
  generation: number;
  /** Every session the manifest recorded for this generation, in file order. */
  sessions: SnapshotSession[];
  /** The generation `state.json` currently describes. */
  isCurrent: boolean;
  status: StatusStyle;
  notes: AttemptNote[];
}

export interface AttemptRow {
  group: SnapshotGroup;
  /** One entry per column; `null` where the group has no such generation. */
  cells: (AttemptCell | null)[];
  /** Attempts actually recorded — the board-level "more than one" signal. */
  generationCount: number;
  retiredSessionCount: number;
}

export interface AttemptGridModel {
  /** Column headers: generation numbers, ascending, across all groups. */
  generations: number[];
  rows: AttemptRow[];
}

export interface AttemptGridOptions {
  escalations?: EscalationRequest[];
  /** Injected in tests; `Date.now()` in the app. */
  nowMs?: number;
}

// ------------------------------------------------------------------ helpers

/** Generations with a recorded session, plus the one the state is describing.
 *
 * The union matters in both directions: a respawned group has sessions for a
 * generation the state has already moved past, and a group that has just been
 * respawned has a current generation with no session written yet. Dropping
 * either would under-report attempts, which is the one thing this grid exists
 * to stop. */
export function generationsOf(group: SnapshotGroup): number[] {
  const seen = new Set<number>(group.sessions.map((s) => s.generation));
  seen.add(group.generation);
  return [...seen].sort((a, b) => a - b);
}

export function sessionsInGeneration(
  group: SnapshotGroup,
  generation: number,
): SnapshotSession[] {
  return group.sessions.filter((s) => s.generation === generation);
}

/** The circuit breaker's retirement text, as `sessions.py` writes it. */
export function isBreakerRetirement(reason: string | null | undefined): boolean {
  return Boolean(reason && /context tokens\s+\d+\s+exceeded limit\s+\d+/i.test(reason));
}

/**
 * A `claude -p` outage rather than a fault in the work.
 *
 * Two shapes reach the manifest: the API's own usage-limit text, and the
 * `LlmProcessError` the orchestrator raises when the CLI dies without ever
 * printing an envelope. Both mean "the harness went away", which is a wait,
 * not a rewrite.
 *
 * This is the **display** mirror of `sessions.py`'s `_USAGE_LIMIT_RE`, and
 * deliberately not the same pattern. That one decides control flow — whether a
 * run pauses and retries — and must only match evidence actually observed on
 * the wire. This one labels a *recorded failure string* after the fact, so it
 * is looser on purpose and also matches the orchestrator's own error class
 * names, which never appear in a live envelope. Three divergent pattern sets
 * already existed before this comment; keeping them honestly separate is
 * better than a shared regex that would have to serve both jobs badly. If you
 * change one, check whether the other actually wants the same change — usually
 * it does not.
 */
export function isUsageLimitOutage(text: string | null | undefined): boolean {
  if (!text) return false;
  return /usage limit|rate.?limit|LlmProcessError|claude -p failed/i.test(text);
}

/** Milliseconds, or null for an absent/unparseable timestamp. */
export function timeMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}

/**
 * The most recent write this group is known to have made.
 *
 * Heartbeat first, transcript mtimes second — both are facts recorded by the
 * runner for free. `live_pids` is deliberately not an input here or anywhere
 * else in this feature: a pid says a process exists, not that a group is
 * progressing, and a run whose orchestrator crashed must render exactly like a
 * finished one.
 */
export function lastWriteMs(group: SnapshotGroup): number | null {
  const candidates = [
    timeMs(group.heartbeat?.updated_at),
    timeMs(group.heartbeat?.round_started_at),
    timeMs(group.heartbeat?.started_at),
    ...group.sessions.map((s) => timeMs(s.transcript_mtime)),
    ...group.sessions.map((s) => timeMs(s.ended_at)),
  ].filter((value): value is number => value !== null);
  return candidates.length === 0 ? null : Math.max(...candidates);
}

/**
 * The command that continues an interrupted run.
 *
 * Rendered as copyable text and never as a button: launching, resuming and
 * aborting runs from the UI is an explicit non-goal, and a button is exactly
 * the thing that would cross it. Showing the string the operator would type is
 * not run control — it is documentation they can paste.
 */
export function resumeCommand(runId: string): string {
  return `smart-mcps-orchestrate resume ${runId}`;
}

/**
 * Whether this session was written by a manifest that records actuals at all.
 *
 * `started_at` and the four token classes arrived together; a run written
 * before them has sessions with neither. Absence of the fields is not the same
 * observation as a round that failed to save, and conflating the two would
 * label every historical run as broken.
 */
export function hasActuals(session: SnapshotSession): boolean {
  return session.started_at != null || session.ended_at != null;
}

const BOOKKEEPING_ELAPSED_MS = 60_000;

/**
 * A session with real elapsed time but no completed round.
 *
 * Round bookkeeping is written when a round *saves*, so a round interrupted
 * partway leaves zero record of itself — the group looks like it did nothing
 * for an hour. It did not; the evidence (elapsed time, tokens spent, commits in
 * the worktree) simply outlived the record. The grid says "bookkeeping lost",
 * which is what happened, rather than "0 rounds", which reads as idleness.
 */
export function lostBookkeeping(session: SnapshotSession): boolean {
  if (!hasActuals(session)) return false;
  if (session.rounds_completed > 0) return false;
  const started = timeMs(session.started_at);
  const ended = timeMs(session.ended_at);
  const elapsed = started !== null && ended !== null ? ended - started : 0;
  const spent = session.total_output_tokens > 0 || session.last_context_tokens > 0;
  return elapsed >= BOOKKEEPING_ELAPSED_MS || spent;
}

// -------------------------------------------------------------------- notes

function cellNotes(
  group: SnapshotGroup,
  generation: number,
  sessions: SnapshotSession[],
  isCurrent: boolean,
  runId: string,
  escalations: EscalationRequest[],
  nowMs: number,
): AttemptNote[] {
  const notes: AttemptNote[] = [];

  // 1 — breaker retirement. The session was retired for context growth, which
  // is a property of the conversation, not a verdict on the work.
  for (const session of sessions) {
    if (isBreakerRetirement(session.retirement_reason)) {
      notes.push({
        kind: "breaker_retirement",
        text: `${session.role} retired by the circuit breaker`,
        detail: session.retirement_reason ?? "",
      });
    }
  }

  // 2 — usage-limit outage. Read from the retirement reasons of this
  // generation, and from the group's `failure` only when that failure still
  // describes the group (see the stale-failure note below).
  const outageTexts = [
    ...sessions.map((s) => s.retirement_reason),
    isCurrent && !group.stale_failure ? group.failure : null,
  ].filter(isUsageLimitOutage);
  if (outageTexts.length > 0) {
    notes.push({
      kind: "usage_limit_outage",
      text: "the claude process went away — an outage, not a fault in the work",
      detail: outageTexts[0] ?? "",
    });
  }

  // 3 — escalation-blocked, which is orthogonal to state: a group can be
  // blocked in any of the busy states, so this is a chip and never the cell's
  // colour. Solid amber, because the operator is being asked something.
  const pending = escalations.filter(
    (esc) => esc.group_id === group.group_id && esc.generation === generation,
  );
  for (const esc of pending) {
    notes.push({
      kind: "escalation_blocked",
      text: `blocked on the operator (${esc.kind})`,
      detail: esc.prompt,
    });
  }

  // 4 — superseded by respawn. An earlier generation was not "cancelled" and
  // did not fail on its own terms; a later attempt replaced it.
  if (!isCurrent && generation < group.generation) {
    notes.push({
      kind: "superseded_by_respawn",
      text: `superseded by generation ${group.generation}`,
      detail:
        "A respawn starts a fresh generation. This attempt's sessions are kept " +
        "because the manifest is append-only — this is the history, not a failure.",
    });
  }

  // 5 — self_verify with no reviewer. Correct, not missing data: the intensity
  // the grouper assigned decides how many reviewer sessions to expect.
  if (
    group.intensity === "self_verify" &&
    sessions.length > 0 &&
    !sessions.some((s) => s.role === "reviewer")
  ) {
    notes.push({
      kind: "self_verify_no_reviewer",
      text: "self_verify — no reviewer session is expected",
      detail: "The grouper rated this group self_verify, so the coder verifies its own work.",
    });
  }

  // 6 — interrupted, and therefore resumable. Copyable text, never a button.
  if (isCurrent && group.state === "interrupted") {
    notes.push({
      kind: "interrupted_resumable",
      text: "the harness died under this group — resumable",
      detail:
        "Resumes the whole run. Pass the run's escalation flags again if you " +
        "overrode them; an omitted flag restores the persisted tier.",
      copyable: resumeCommand(runId),
    });
  }

  // 7 — round-atomic bookkeeping loss, and its quieter cousin: a manifest
  // written before actuals were recorded at all.
  for (const session of sessions) {
    if (lostBookkeeping(session)) {
      notes.push({
        kind: "bookkeeping_lost",
        text: `${session.role}: round bookkeeping lost`,
        detail:
          "Bookkeeping is written when a round saves, so a round interrupted " +
          "partway leaves no record of itself. Elapsed time and commits may " +
          "well exist — this is a lost record, not an idle session.",
      });
    }
  }
  if (sessions.length > 0 && sessions.every((s) => !hasActuals(s))) {
    notes.push({
      kind: "actuals_missing",
      text: "actuals not recorded for this run",
      detail: "This manifest predates per-session timing and token accounting.",
    });
  }

  // The likeliest wrong thing this surface could do: `GroupRunState` is
  // last-writer-wins, so a group that failed and was then resolved keeps the
  // old failure string hanging off a successful state. It is history, and it
  // is shown as history — never as a failure and never as a failure colour.
  if (isCurrent && group.failure && group.stale_failure) {
    notes.push({
      kind: "stale_failure",
      text: "stale failure text",
      detail:
        `${group.failure} — this text is from an earlier attempt. ` +
        "state.json is single-valued and cannot say \"failed once, then succeeded\", " +
        `so the string survived into the ${group.state} state. The group is ${group.state}.`,
    });
  }

  // An inference, and the UI says so: a `?` overlay with the elapsed fact, not
  // a colour and not the word "hung".
  if (isCurrent) {
    const stall = inferStall(group, lastWriteMs(group), pending.length > 0, nowMs);
    if (stall.stalled) {
      notes.push({
        kind: "stalled",
        text: stall.note,
        detail:
          "An inference, not a state: the group is in an active state and " +
          "nothing in the run directory has been written recently. It may " +
          "still be working.",
      });
    }
  }

  return notes;
}

// --------------------------------------------------------------------- grid

export function buildAttemptGrid(
  snapshot: RunSnapshot,
  options: AttemptGridOptions = {},
): AttemptGridModel {
  const escalations = options.escalations ?? [];
  const nowMs = options.nowMs ?? Date.now();

  const columns = new Set<number>();
  for (const group of snapshot.groups) {
    for (const generation of generationsOf(group)) columns.add(generation);
  }
  const generations = [...columns].sort((a, b) => a - b);

  const rows = snapshot.groups.map((group) => {
    const own = new Set(generationsOf(group));
    const cells = generations.map<AttemptCell | null>((generation) => {
      if (!own.has(generation)) return null;
      const sessions = sessionsInGeneration(group, generation);
      const isCurrent = generation === group.generation;
      return {
        groupId: group.group_id,
        generation,
        sessions,
        isCurrent,
        // The current column takes the group's state; every earlier one is
        // superseded, because state.json has nothing to say about it.
        status: isCurrent ? statusOf(group.state) : SUPERSEDED_STATUS,
        notes: cellNotes(
          group,
          generation,
          sessions,
          isCurrent,
          snapshot.run_id,
          escalations,
          nowMs,
        ),
      };
    });
    return {
      group,
      cells,
      generationCount: own.size,
      retiredSessionCount: group.sessions.filter((s) => s.retirement_reason).length,
    };
  });

  return { generations, rows };
}

/**
 * The board-level signal that earlier attempts exist.
 *
 * The board shows one generation number, so nothing on it hints that three
 * retired sessions are sitting in the manifest — you had to drill into each
 * group to find out. This is the summary the card shows instead, and it counts
 * sessions, because the manifest is what knows.
 */
export interface AttemptSummary {
  generations: number;
  sessions: number;
  retired: number;
  /** Whether the card should show the signal at all. */
  hasHistory: boolean;
  label: string;
}

export function summariseAttempts(group: SnapshotGroup): AttemptSummary {
  const generations = generationsOf(group).length;
  const sessions = group.sessions.length;
  const retired = group.sessions.filter((s) => s.retirement_reason).length;
  const parts: string[] = [];
  if (generations > 1) parts.push(`${generations} generations`);
  if (retired > 0) parts.push(`${retired} retired ${retired === 1 ? "session" : "sessions"}`);
  return {
    generations,
    sessions,
    retired,
    hasHistory: generations > 1 || retired > 0,
    label: parts.join(" · "),
  };
}

/**
 * A session's display name — `session_display_name` in `sessions.py` — is
 * `{run_id}-{group_id}-{role}-g{generation}`, e.g.
 * `r20260820-213134-g1-coder-g3`. Read literally, that trailing `-g3` reads as
 * though it names a group, not a generation (plan U35/F17) — the same digits
 * as `g1` earlier in the string, easy to mistake for "this session touches
 * group g3". `sessionGeneration` pulls the number back out so callers can
 * render it as its own labelled badge instead, and `sessionBaseName` gives the
 * name with that suffix removed so it is not printed twice.
 *
 * The base session (`{run_id}-base`, no trailing `-g<N>`) has no generation to
 * extract, and that must render as "no label" — never `gen 0`.
 */
const SESSION_GENERATION_RE = /-g(\d+)$/;

export function sessionGeneration(name: string): number | null {
  const match = SESSION_GENERATION_RE.exec(name);
  return match ? Number(match[1]) : null;
}

export function sessionBaseName(name: string): string {
  return name.replace(SESSION_GENERATION_RE, "");
}
