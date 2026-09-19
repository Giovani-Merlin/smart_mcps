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

describe("the board shows the liveness line and activity tail (plan U7)", () => {
  function snapshotWith(
    heartbeat: RunSnapshot["groups"][number]["heartbeat"],
    activityTail?: RunSnapshot["groups"][number]["sessions"][number]["activity_tail"],
  ): RunSnapshot {
    return {
      ...R20260726_GROUPING,
      groups: R20260726_GROUPING.groups.map((group) =>
        group.group_id === "g1"
          ? {
              ...group,
              heartbeat,
              sessions:
                activityTail === undefined
                  ? group.sessions
                  : group.sessions.map((session, index) =>
                      index === group.sessions.length - 1
                        ? { ...session, activity_tail: activityTail }
                        : session,
                    ),
            }
          : group,
      ),
    };
  }

  function boardWith(
    heartbeat: RunSnapshot["groups"][number]["heartbeat"],
    activityTail?: RunSnapshot["groups"][number]["sessions"][number]["activity_tail"],
  ) {
    const snapshot = snapshotWith(heartbeat, activityTail);
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

  it("renders NOT LIVE with the evidence text and the not-live class when 20 minutes past the window", () => {
    const nowS = Date.now() / 1000;
    boardWith({
      generation: 1,
      round: 1,
      phase: "round 1 running",
      child_pid: 4242,
      child_spawned_at: new Date((nowS - 30 * 60) * 1000).toISOString(),
      last_sign_of_life_at: new Date((nowS - 20 * 60) * 1000).toISOString(),
      sign_of_life_signal: "cpu",
      sign_of_life_evidence: "cpu flat for 20m",
      liveness_window_s: 600,
    });
    const card = cardOf("g1");
    const liveness = card.querySelector(".group-card__liveness")!;
    expect(liveness.textContent).toContain("NOT LIVE for");
    expect(liveness.textContent).toContain("cpu flat for 20m");
    expect(liveness.classList.contains("group-card__liveness--not-live")).toBe(true);
  });

  it("renders a fresh child as live:, not not-live", () => {
    const nowS = Date.now() / 1000;
    boardWith({
      generation: 1,
      round: 1,
      phase: "round 1 running",
      child_pid: 4242,
      child_spawned_at: new Date((nowS - 30) * 1000).toISOString(),
      last_sign_of_life_at: new Date((nowS - 5) * 1000).toISOString(),
      sign_of_life_signal: "event tool_use",
      sign_of_life_evidence: "event tool_use 5s ago",
      liveness_window_s: 600,
    });
    const card = cardOf("g1");
    const liveness = card.querySelector(".group-card__liveness")!;
    expect(liveness.textContent).toContain("live:");
    expect(liveness.classList.contains("group-card__liveness--not-live")).toBe(false);
  });

  it("renders no liveness line at all for a pre-liveness fixture group, and no error", () => {
    board();
    expect(cardOf("g1").querySelector(".group-card__liveness")).toBeNull();
  });

  it("renders the newest session's activity tail", () => {
    boardWith(null, [
      { at: "2026-08-09T10:00:00Z", tool: "Bash", input_head: "uv run pytest", returned: true },
      { at: "2026-08-09T10:01:00Z", tool: "Edit", input_head: "runs.py", returned: false },
    ]);
    const card = cardOf("g1");
    const tail = card.querySelector(".group-card__activity")!;
    expect(tail.textContent).toContain("uv run pytest");
    expect(tail.textContent).toContain("returned");
    expect(tail.textContent).toContain("Edit");
    expect(tail.textContent).toContain("running");
  });

  it("renders no activity block for a pre-liveness fixture group, and no error", () => {
    board();
    expect(cardOf("g1").querySelector(".group-card__activity")).toBeNull();
  });
});
