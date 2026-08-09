// Pure helpers behind the pipeline stepper.
//
// Kept out of the component because this is the part that has to be *right*:
// the stepper is the closest thing to a stored answer for "why is task X in
// group Y", and a colouring bug here would be an authoritative-looking lie.
//
// The backend already computes the recolour sets (`stage_diffs`, by
// co-membership rather than group id — `renumber` relabels every group without
// moving a single task). Nothing is recomputed here; these functions only turn
// a stage index into what the graph should draw.

import type { GroupingView, StageDiff, StageSnapshot } from "../../types";

/** A stable colour index per group id, so scrubbing does not reshuffle hues. */
export type Palette = Map<number, number>;

export interface StageFrame {
  index: number;
  stage: string;
  /** node → group id at this stage. */
  partition: Record<string, number>;
  /** Nodes whose group-mates changed at this stage; the recolour set. */
  moved: Set<string>;
  diff: StageDiff | null;
  groupCount: number;
}

/**
 * The frames the stepper scrubs through.
 *
 * Driven entirely by `stages` and `stage_diffs` as they arrived — the stage
 * *names* are data, not a hardcoded list. Real traces on disk disagree about
 * the first stage's name (`contraction` in one, `louvain` in another), and a
 * hardcoded pipeline would silently mis-label one of them.
 */
export function buildFrames(view: GroupingView): StageFrame[] {
  const diffs = new Map(view.stage_diffs.map((diff) => [diff.stage, diff]));
  return view.stages.map((snapshot: StageSnapshot, index: number) => {
    const diff = diffs.get(snapshot.stage) ?? null;
    return {
      index,
      stage: snapshot.stage,
      partition: snapshot.partition,
      moved: new Set(diff?.moved ?? []),
      diff,
      groupCount: diff?.group_count ?? countGroups(snapshot.partition),
    };
  });
}

function countGroups(partition: Record<string, number>): number {
  return new Set(Object.values(partition)).size;
}

/**
 * A stable hue index for every group id that appears anywhere in the run.
 *
 * Assigned once across all stages rather than per stage: if group 3 is teal at
 * `split` and orange at `merge` purely because the id set changed size, the
 * scrub reads as chaos and the four tasks that actually moved are lost in it.
 */
export function buildPalette(frames: StageFrame[], size: number): Palette {
  const ids = new Set<number>();
  for (const frame of frames) {
    for (const id of Object.values(frame.partition)) ids.add(id);
  }
  const palette: Palette = new Map();
  [...ids]
    .sort((a, b) => a - b)
    .forEach((id, index) => palette.set(id, index % size));
  return palette;
}

/** Which stage index a `?stage=` query param refers to; -1 when unknown. */
export function stageIndexOf(frames: StageFrame[], stage: string | null): number {
  if (!stage) return frames.length > 0 ? frames.length - 1 : -1;
  const byName = frames.findIndex((frame) => frame.stage === stage);
  if (byName >= 0) return byName;
  const asNumber = Number.parseInt(stage, 10);
  if (Number.isInteger(asNumber) && asNumber >= 0 && asNumber < frames.length) {
    return asNumber;
  }
  return frames.length > 0 ? frames.length - 1 : -1;
}

/** Members of one group at one stage, in stable order. */
export function membersOf(frame: StageFrame, groupId: number): string[] {
  return Object.entries(frame.partition)
    .filter(([, id]) => id === groupId)
    .map(([node]) => node)
    .sort();
}

/**
 * A one-line account of what a stage did, phrased as an observation.
 *
 * "4 tasks changed group" is a fact. "the merge stage fixed the partition" is a
 * claim, and this surface does not make claims.
 */
export function describeStage(frame: StageFrame): string {
  if (!frame.diff || frame.diff.previous_stage === null) {
    return `${Object.keys(frame.partition).length} tasks, ${frame.groupCount} groups`;
  }
  const moved = frame.moved.size;
  const groups = `${frame.groupCount} group${frame.groupCount === 1 ? "" : "s"}`;
  if (moved === 0) return `no task changed group · ${groups}`;
  return `${moved} task${moved === 1 ? "" : "s"} changed group · ${groups}`;
}

/**
 * Affinity edges, heaviest first, joined to the current partition.
 *
 * The weight is the *sum* of every signal that contributed — shared files,
 * codegraph calls, declared dependencies, semantic tags. Which signals those
 * were is not recoverable from the trace; that is what `edge-provenance.json`
 * would carry, and why its absence gets a named degradation rather than a
 * silent omission.
 */
export function rankedEdges(
  view: GroupingView,
  frame: StageFrame | null,
): Array<{ from: string; to: string; weight: number; crossGroup: boolean }> {
  const affinity = view.input_graph?.affinity ?? [];
  return affinity
    .map(([from, to, weight]) => ({
      from,
      to,
      weight,
      crossGroup: frame ? frame.partition[from] !== frame.partition[to] : false,
    }))
    .sort((a, b) => b.weight - a.weight);
}

export function edgeKey(from: string, to: string): string {
  return `${from}→${to}`;
}
