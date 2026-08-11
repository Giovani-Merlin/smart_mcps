// The status map is the surface that rots first: a state added to the
// orchestrator and forgotten here renders as a blank badge, which looks like it
// worked. `tsc` catches the union case; these cover what it cannot.

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { GROUP_STATES } from "./types";
import type { SnapshotGroup } from "./types";
import {
  ACTIVE_STATES,
  ATTENTION_COLOUR,
  STATUS,
  SUPERSEDED_STATUS,
  UNKNOWN_STATUS,
  failureIsCurrent,
  formatDuration,
  inferStall,
  statusOf,
} from "./status";

function group(over: Partial<SnapshotGroup> = {}): SnapshotGroup {
  return {
    group_id: "g1",
    name: "g1",
    summary: "",
    state: "running",
    generation: 1,
    failure: null,
    stale_failure: false,
    depends_on: [],
    sessions: [],
    ...over,
  };
}

describe("status map", () => {
  it("styles every state the orchestrator can produce", () => {
    for (const state of GROUP_STATES) {
      const style = statusOf(state);
      expect(style.label, `${state} has no label`).toBeTruthy();
      expect(style.colour, `${state} has no colour`).toBeTruthy();
      expect(style.glyph, `${state} has no glyph`).toBeTruthy();
    }
    expect(Object.keys(STATUS).sort()).toEqual([...GROUP_STATES].sort());
  });

  it("gives a state from a newer backend a visible badge, not a blank one", () => {
    expect(statusOf("teleported")).toBe(UNKNOWN_STATUS);
    expect(UNKNOWN_STATUS.label).toBe("unknown state");
  });

  it("keeps resolved distinct from completed and from failed", () => {
    expect(statusOf("resolved").colour).not.toBe(statusOf("completed").colour);
    expect(statusOf("resolved").colour).not.toBe(statusOf("failed").colour);
  });

  it("marks interrupted as unfinished without marking it wrong", () => {
    expect(statusOf("interrupted").dashed).toBe(true);
    expect(statusOf("interrupted").colour).not.toBe(statusOf("failed").colour);
  });

  it("collapses the four busy states to one hue told apart by glyph", () => {
    const busy = ACTIVE_STATES.map(statusOf);
    expect(new Set(busy.map((s) => s.colour)).size).toBe(1);
    expect(new Set(busy.map((s) => s.glyph)).size).toBe(busy.length);
  });
});

describe("stale failure text", () => {
  it("does not treat a resolved group's leftover failure as current", () => {
    const resolved = group({ state: "resolved", failure: "reviewer said structural", stale_failure: true });
    expect(failureIsCurrent(resolved)).toBe(false);
  });

  it("does treat an interrupted group's failure as current", () => {
    const interrupted = group({ state: "interrupted", failure: "usage limit reached" });
    expect(failureIsCurrent(interrupted)).toBe(true);
  });
});

describe("stall inference", () => {
  const now = 1_000_000_000;

  it("says nothing about a group that is not active", () => {
    expect(inferStall(group({ state: "completed" }), now - 3_600_000, false, now).stalled).toBe(false);
  });

  it("calls a long-quiet active group stalled, as an observation", () => {
    const result = inferStall(group(), now - 23 * 60_000, false, now);
    expect(result.stalled).toBe(true);
    expect(result.note).toBe("no activity for 23m");
    expect(result.note).not.toMatch(/hung|dead|stuck/);
  });

  it("does not call a group waiting on the operator stalled", () => {
    const result = inferStall(group(), now - 3_600_000, true, now);
    expect(result.stalled).toBe(false);
    expect(result.note).toBe("waiting on the operator");
  });

  it("stays quiet below the threshold", () => {
    expect(inferStall(group(), now - 60_000, false, now).stalled).toBe(false);
  });

  it("formats hours as well as minutes", () => {
    expect(formatDuration(90 * 60_000)).toBe("1h 30m");
  });
});

// ------------------------------------------------------------------ tokens

// The colours are `var(--token)` references with no fallback, so a token that
// `tokens.css` does not define resolves to nothing and the badge renders
// invisible — which is the exact failure `status.ts` exists to prevent, just
// moved one layer down. This closes that gap.
describe("the token layer behind the status map", () => {
  const tokens = readFileSync("src/tokens.css", "utf8");

  function tokensUsedBy(value: string): string[] {
    return [...value.matchAll(/var\((--[\w-]+)\)/g)].map((match) => match[1]);
  }

  it("defines every token the status map names", () => {
    const referenced = new Set(
      [
        ...Object.values(STATUS),
        UNKNOWN_STATUS,
        SUPERSEDED_STATUS,
        { colour: ATTENTION_COLOUR } as { colour: string },
      ].flatMap((style) => tokensUsedBy(style.colour)),
    );
    expect(referenced.size).toBeGreaterThan(0);
    for (const token of referenced) {
      expect(tokens, `${token} is referenced but never defined`).toContain(`${token}:`);
    }
  });

  it("keeps amber to the one meaning it is allowed to have", () => {
    // Amber means "needs the operator's attention". A state is not attention:
    // a group can be running and blocked at the same time, and folding the two
    // into one colour would lose which.
    for (const [state, style] of Object.entries(STATUS)) {
      expect(style.colour, `${state} must not be amber`).not.toBe(ATTENTION_COLOUR);
    }
    expect(ATTENTION_COLOUR).toBe("var(--status-attention)");
  });
});

// The same guard, widened to the whole bundle: any `var(--token)` a module
// names must exist in `tokens.css`. Fallbacks were stripped deliberately — with
// the token layer always loaded, a fallback is a second place the colour is
// written down — which means an undefined token now resolves to nothing at all.
describe("the token layer as a whole", () => {
  it("defines every token any module references", () => {
    const tokens = readFileSync("src/tokens.css", "utf8");
    const sources = ["src/status.ts", "src/cost.ts"];
    for (const file of sources) {
      const source = readFileSync(file, "utf8");
      for (const [, token] of source.matchAll(/var\((--[\w-]+)\)/g)) {
        expect(tokens, `${file} names ${token}, which tokens.css does not define`).toContain(
          `${token}:`,
        );
      }
    }
  });
});
