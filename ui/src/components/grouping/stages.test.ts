// The stepper's helpers, driven by the same real trace the Python tests use.
//
// This matters more than it looks: the stepper is the closest thing to a stored
// answer for "why is task X in group Y", so a colouring bug here is an
// authoritative-looking lie rather than a cosmetic glitch.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import type { GroupingView } from "../../types";
import { buildFrames, buildPalette, describeStage, membersOf, stageIndexOf } from "./stages";

const TRACE = JSON.parse(
  readFileSync(
    join(__dirname, "../../../../tests/fixtures/observatory/run-modern/grouping-trace.json"),
    "utf8",
  ),
);

/** The trace sections the tab consumes, shaped as the API serves them. */
function viewOf(over: Partial<GroupingView> = {}): GroupingView {
  return {
    project: "proj",
    run_id: "modern1",
    plan_path: "docs/plan.md",
    dag_source: { kind: "run_snapshot", stale_dag: false, reason: "" },
    missing: [],
    trace_schema_known: true,
    input_graph: TRACE.input_graph,
    node_work: TRACE.node_work,
    config: TRACE.config,
    hub_roles: TRACE.hub_roles,
    slice_atoms: TRACE.slice_atoms,
    stages: TRACE.stages,
    louvain: TRACE.louvain,
    splits: TRACE.splits,
    merges: TRACE.merges,
    repairs: TRACE.repairs,
    group_difficulty: TRACE.groups,
    scorecard: TRACE.scorecard,
    stage_diffs: diffsOf(TRACE.stages),
    flags: [],
    mapper_flags: [],
    partition_flags: [],
    paths: {},
    ...over,
  } as GroupingView;
}

/** A local mirror of the backend's co-membership diff, so this file does not
 * need the API running to have real recolour sets to assert against. */
function diffsOf(stages: Array<{ stage: string; partition: Record<string, number> }>) {
  const mates = (partition: Record<string, number>) => {
    const byGroup = new Map<number, string[]>();
    for (const [node, id] of Object.entries(partition)) {
      byGroup.set(id, [...(byGroup.get(id) ?? []), node]);
    }
    return new Map(
      Object.entries(partition).map(([node, id]) => [
        node,
        (byGroup.get(id) ?? []).filter((other) => other !== node).sort().join("|"),
      ]),
    );
  };
  let previous: Map<string, string> | null = null;
  let previousName: string | null = null;
  return stages.map((snapshot) => {
    const current = mates(snapshot.partition);
    const moved = previous
      ? [...current.keys()].filter((n) => previous!.has(n) && previous!.get(n) !== current.get(n)).sort()
      : [];
    const added = previous ? [...current.keys()].filter((n) => !previous!.has(n)).sort() : [...current.keys()].sort();
    const diff = {
      stage: snapshot.stage,
      previous_stage: previousName,
      moved,
      added,
      removed: [],
      group_count: new Set(Object.values(snapshot.partition)).size,
    };
    previous = current;
    previousName = snapshot.stage;
    return diff;
  });
}

describe("frames", () => {
  it("takes its stage names from the trace rather than a hardcoded pipeline", () => {
    // Two real traces on disk disagree about the first stage's name
    // (`contraction` vs `louvain`); a hardcoded list mis-labels one of them.
    expect(buildFrames(viewOf()).map((f) => f.stage)).toEqual(
      TRACE.stages.map((s: { stage: string }) => s.stage),
    );
  });

  it("recolours exactly the tasks that changed group at the merge stage", () => {
    const merge = buildFrames(viewOf()).find((f) => f.stage === "merge");
    expect([...(merge?.moved ?? [])].sort()).toEqual([
      "merge-f4-groups-path-fix",
      "observatory-drift-repair",
      "transcript-parser-thinking-usage",
      "ui-grouping-tab",
    ]);
  });

  it("recolours nothing at renumber, where every group id changes", () => {
    const renumber = buildFrames(viewOf()).find((f) => f.stage === "renumber");
    expect([...(renumber?.moved ?? [])]).toEqual([]);
  });

  it("keeps a group's hue stable across stages", () => {
    const frames = buildFrames(viewOf());
    const palette = buildPalette(frames, 8);
    const ids = new Set(frames.flatMap((f) => Object.values(f.partition)));
    for (const id of ids) expect(palette.get(id)).toBeTypeOf("number");
    // Assigned once over every stage, so a stage with fewer groups does not
    // reshuffle the hues of the ones that did not move.
    expect(new Set(palette.values()).size).toBeLessThanOrEqual(8);
  });
});

describe("stage selection", () => {
  const frames = buildFrames(viewOf());

  it("defaults to the final stage when ?stage= is absent", () => {
    expect(stageIndexOf(frames, null)).toBe(frames.length - 1);
  });

  it("resolves a stage name from the query param", () => {
    expect(frames[stageIndexOf(frames, "split")].stage).toBe("split");
  });

  it("falls back to the final stage for a name that is not in this trace", () => {
    expect(stageIndexOf(frames, "contraction")).toBe(frames.length - 1);
  });

  it("accepts a numeric index too, for links made before a rename", () => {
    expect(stageIndexOf(frames, "1")).toBe(1);
  });
});

describe("wording", () => {
  it("states what a stage did as a fact, never as a judgement", () => {
    const frames = buildFrames(viewOf());
    const merge = frames.find((f) => f.stage === "merge")!;
    expect(describeStage(merge)).toBe("4 tasks changed group · 9 groups");
    const renumber = frames.find((f) => f.stage === "renumber")!;
    expect(describeStage(renumber)).toMatch(/^no task changed group/);
  });

  it("lists a group's members in stable order", () => {
    const frame = buildFrames(viewOf())[0];
    const someGroup = Object.values(frame.partition)[0];
    const members = membersOf(frame, someGroup);
    expect(members).toEqual([...members].sort());
  });
});
