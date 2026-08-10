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
