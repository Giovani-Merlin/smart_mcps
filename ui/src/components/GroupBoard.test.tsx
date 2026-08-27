// The board's attempt signal.
//
// The board showed a generation number and nothing else, so a group with three
// retired sessions in the manifest looked exactly like one that ran clean the
// first time. These cover the signal that fixes that, and the stale-failure
// rule the card already carries — both against the real run on disk.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { R20260726_GROUPING } from "../fixtures/r20260726-grouping";
import { statusOf } from "../status";
import type { RunSnapshot } from "../types";
import GroupBoard from "./GroupBoard";

afterEach(cleanup);

function board() {
  return render(
    <GroupBoard
      project="smart-mcps"
      runId={R20260726_GROUPING.run_id}
      snapshot={R20260726_GROUPING}
      revision={1}
      loading={false}
    />,
  );
}

function cardOf(groupId: string): HTMLElement {
  return screen.getByText(groupId).closest(".group-card") as HTMLElement;
}

describe("the board signals that earlier attempts exist", () => {
  it("says so on g2, which has two generations and a retired session", () => {
    board();
    const card = cardOf("g2");
    expect(card.textContent).toContain("2 generations · 1 retired session");
    // Counted from the manifest, so it survives state.json reporting only gen 2.
    const badge = card.querySelector(".group-card__attempts")!;
    expect(badge.getAttribute("title")).toContain("4 sessions recorded in manifest.json");
  });

  it("stays quiet on a group that ran clean the first time", () => {
    board();
    expect(cardOf("g1").querySelector(".group-card__attempts")).toBeNull();
    expect(cardOf("g1").textContent).toContain("gen 1");
  });
});

describe("the board shows the paused portion of a long phase (plan U26)", () => {
  function snapshotWithHeartbeat(heartbeat: RunSnapshot["groups"][number]["heartbeat"]): RunSnapshot {
    return {
      ...R20260726_GROUPING,
      groups: R20260726_GROUPING.groups.map((group) =>
        group.group_id === "g1" ? { ...group, heartbeat } : group,
      ),
    };
  }

  function boardWith(heartbeat: RunSnapshot["groups"][number]["heartbeat"]) {
    const snapshot = snapshotWithHeartbeat(heartbeat);
    return render(
      <GroupBoard
        project="smart-mcps"
        runId={snapshot.run_id}
        snapshot={snapshot}
        revision={1}
        loading={false}
      />,
    );
  }

  it("shows the paused portion distinctly from the phase elapsed", () => {
    boardWith({
      generation: 2,
      round: 3,
      phase: "forking the base session",
      phase_elapsed_s: 3529,
      paused_s: 3472,
      round_elapsed_s: 1380,
    });
    const card = cardOf("g1");
    const phase = card.querySelector(".group-card__phase")!;
    expect(phase.textContent).toContain("forking the base session");
    expect(phase.textContent).toContain("58m");
    const paused = phase.querySelector(".group-card__paused")!;
    expect(paused.textContent).toContain("57m");
    // Distinct DOM node, not folded into the phase-elapsed text.
    expect(phase.textContent).not.toBe(paused.textContent);
  });

  it("renders without the paused chip, not a fabricated zero, when paused_s is absent", () => {
    boardWith({
      generation: 2,
      round: 3,
      phase: "forking the base session",
      phase_elapsed_s: 3529,
      paused_s: null,
      round_elapsed_s: null,
    });
    const card = cardOf("g1");
    expect(card.querySelector(".group-card__paused")).toBeNull();
    expect(card.textContent).not.toContain("0m paused");
    expect(card.querySelector(".group-card__phase")!.textContent).toContain("58m");
  });

  it("renders no phase line at all when the heartbeat has none", () => {
    boardWith(null);
    expect(cardOf("g1").querySelector(".group-card__phase")).toBeNull();
  });
});

describe("the board does not call a stale failure a failure", () => {
  it("shows g3's leftover failure text as history under a completed state", () => {
    board();
    const card = cardOf("g3");
    expect(card.querySelector(".group-card__stale-failure")).toBeTruthy();
    expect(card.querySelector(".group-card__failure")).toBeNull();
    expect(card.textContent).toContain("stale failure text");
    expect(card.textContent).toContain(statusOf("completed").label);
  });
});
