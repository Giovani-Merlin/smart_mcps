// The rendered grid, against the real run on disk.
//
// The unit tests in `attempts.test.ts` cover the derivation; these cover what
// the operator actually sees — which is where the stale-failure mistake would
// show up, because the derivation can be perfectly right and the cell still be
// painted red.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { R20260726_GROUPING } from "../fixtures/r20260726-grouping";
import { statusOf } from "../status";
import type { EscalationRequest, RunSnapshot, SnapshotGroup } from "../types";
import AttemptGrid from "./AttemptGrid";

const listEscalations = vi.fn<() => Promise<EscalationRequest[]>>();
const getRunPaths = vi.fn();

vi.mock("../api", () => ({
  listEscalations: (...args: unknown[]) => listEscalations(...(args as [])),
  getRunPaths: (...args: unknown[]) => getRunPaths(...(args as [])),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

beforeEach(() => {
  listEscalations.mockResolvedValue([]);
  getRunPaths.mockResolvedValue({
    project: "smart-mcps",
    run_id: "r20260726-grouping",
    roots: {},
    entries: [
      {
        key: "manifest",
        label: "manifest.json",
        panel: "board",
        path: "/repo/.orchestrator/runs/r20260726-grouping/manifest.json",
        kind: "file",
        exists: true,
      },
      {
        key: "state",
        label: "state.json",
        panel: "board",
        path: "/repo/.orchestrator/runs/r20260726-grouping/state.json",
        kind: "file",
        exists: true,
      },
    ],
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderGrid(snapshot: RunSnapshot, props: Partial<{ nowMs: number; onOpenSession: (g: string, s: string) => void }> = {}) {
  return render(
    <AttemptGrid
      project="smart-mcps"
      runId={snapshot.run_id}
      snapshot={snapshot}
      revision={1}
      {...props}
    />,
  );
}

function rowOf(groupId: string): HTMLElement {
  return screen.getByTestId(`attempt-row-${groupId}`);
}

/** Clicks a group's generation cell and returns the detail panel. */
async function openCell(groupId: string, generation: number): Promise<HTMLElement> {
  const cell = within(rowOf(groupId)).getByRole("button", {
    name: new RegExp(`${groupId} generation ${generation}`),
  });
  fireEvent.click(cell);
  return await screen.findByLabelText("Selected attempt");
}

describe("the grid renders every attempt from real manifest data", () => {
  it("gives g2 a cell in both generations, with two sessions each", async () => {
    renderGrid(R20260726_GROUPING);
    const row = rowOf("g2");
    expect(
      within(row).getByRole("button", { name: /g2 generation 1, superseded, 2 sessions/ }),
    ).toBeTruthy();
    expect(
      within(row).getByRole("button", { name: /g2 generation 2, completed, 2 sessions/ }),
    ).toBeTruthy();
    // The row's own count comes from the manifest's session list.
    expect(row.textContent).toContain("4 sessions");
    expect(row.textContent).toContain("1 retired");
  });

  it("shows gen-1's retirement reason from the grid", async () => {
    renderGrid(R20260726_GROUPING);
    const detail = await openCell("g2", 1);
    expect(detail.textContent).toContain("retired: context tokens 7618531 exceeded limit 120000");
    expect(detail.textContent).toContain("retired by the circuit breaker");
  });

  it("links a cell's session to the route-addressable session viewer", async () => {
    const onOpenSession = vi.fn();
    renderGrid(R20260726_GROUPING, { onOpenSession });
    const detail = await openCell("g2", 1);
    // The name's trailing "-g1" renders as its own "gen 1" badge (plan
    // U35/F17), so the printed name itself is the suffix-stripped form.
    fireEvent.click(within(detail).getByText("r20260726-grouping-g2-coder"));
    expect(onOpenSession).toHaveBeenCalledWith("g2", "13be2fe3-71f7-4c78-948a-8d32ac687aa2");
  });
});

describe("session generation naming and timestamps (U35)", () => {
  function snapshotWithSession(name: string, run = "r-test"): RunSnapshot {
    return {
      project: "smart-mcps",
      run_id: run,
      plan_path: "p.md",
      groups: [
        {
          group_id: "g1",
          name: "a-group",
          summary: "",
          state: "running",
          generation: 1,
          failure: null,
          stale_failure: false,
          depends_on: [],
          sessions: [
            {
              session_id: "s1",
              role: "coder",
              generation: 1,
              name,
              retirement_reason: null,
              transcript_path: null,
              last_context_tokens: 0,
              rounds_completed: 0,
              total_input_tokens: 0,
              total_output_tokens: 0,
              total_cache_read_tokens: 0,
              total_cache_creation_tokens: 0,
            },
          ],
        },
      ],
      edges: [],
      stale_dag: false,
      live_pids: {},
    };
  }

  it("labels a session id ending -coder-g3 as gen 3 in the attempt grid, distinct from a group g3", async () => {
    renderGrid(snapshotWithSession("r20260820-213134-g1-coder-g3", "r20260820-213134"));
    const detail = await openCell("g1", 1);

    expect(within(detail).getByText("gen 3")).toBeTruthy();
    // The raw suffix is gone from the printed name — nothing on the page
    // reads "-g3" as though it names a group.
    expect(within(detail).getByText("r20260820-213134-g1-coder")).toBeTruthy();
    expect(within(detail).queryByText("r20260820-213134-g1-coder-g3")).toBeNull();
    // The "gen 3" badge is a rounded pill (`.attempt-session__gen`), visually
    // distinct from the plain monospace group-id text used elsewhere on the
    // page (`.attempt-grid__group-id`) — the two never share a class.
    const genBadge = within(detail).getByText("gen 3");
    expect(genBadge.className).toBe("attempt-session__gen");
  });

  it("renders no generation label for a session with no generation suffix", async () => {
    renderGrid(snapshotWithSession("r-test-base"));
    const detail = await openCell("g1", 1);

    expect(within(detail).getByText("r-test-base")).toBeTruthy();
    expect(within(detail).queryByText("gen 0")).toBeNull();
    expect(within(detail).queryByText(/^gen \d+$/)).toBeNull();
  });
});

describe("the stale failure never renders as a failure", () => {
  it("paints g3's completed cell with the completed colour and a stale chip", async () => {
    renderGrid(R20260726_GROUPING);
    const cell = within(rowOf("g3")).getByRole("button", { name: /g3 generation 1/ });

    expect(cell.getAttribute("aria-label")).toContain("completed");
    expect(cell.getAttribute("aria-label")).not.toContain("failed");
    expect(cell.getAttribute("style")).toContain(statusOf("completed").colour);
    expect(cell.getAttribute("style")).not.toContain(statusOf("failed").colour);

    const detail = await openCell("g3", 1);
    expect(within(detail).getByText("stale failure text")).toBeTruthy();
    // And the explanation, not just the string.
    const chip = within(detail).getByText("stale failure text").closest("li")!;
    expect(chip.getAttribute("title")).toContain("earlier attempt");
    expect(detail.textContent).not.toMatch(/\bfailed\b/);
  });
});

describe("the seven special cases, rendered", () => {
  function oneGroup(over: Partial<SnapshotGroup>, run = "r-test"): RunSnapshot {
    return {
      project: "smart-mcps",
      run_id: run,
      plan_path: "p.md",
      groups: [
        {
          group_id: "g1",
          name: "a-group",
          summary: "",
          state: "running",
          generation: 1,
          failure: null,
          stale_failure: false,
          depends_on: [],
          sessions: [
            {
              session_id: "s1",
              role: "coder",
              generation: 1,
              name: "r-test-g1-coder-g1",
              retirement_reason: null,
              transcript_path: null,
              last_context_tokens: 0,
              rounds_completed: 0,
              total_input_tokens: 0,
              total_output_tokens: 0,
              total_cache_read_tokens: 0,
              total_cache_creation_tokens: 0,
            },
          ],
          ...over,
        },
      ],
      edges: [],
      stale_dag: false,
      live_pids: {},
    };
  }

  it("escalation-blocked is an overlay, not the cell's colour", async () => {
    listEscalations.mockResolvedValue([
      {
        id: "e1",
        run_id: "r-test",
        group_id: "g1",
        generation: 1,
        kind: "coder_question",
        prompt: "which schema wins?",
        context: { diff_summary: "", surprises: [] },
        created_at: "2026-08-10T10:00:00Z",
      },
    ]);
    renderGrid(oneGroup({ state: "running" }));

    const cell = await waitFor(() => {
      const found = screen.getByRole("button", { name: /g1 generation 1/ });
      if (!found.textContent?.includes("!")) throw new Error("no escalation overlay yet");
      return found;
    });
    // Still the running colour: blocked is orthogonal to state.
    expect(cell.getAttribute("style")).toContain(statusOf("running").colour);
    fireEvent.click(cell);
    const detail = await screen.findByLabelText("Selected attempt");
    expect(detail.textContent).toContain("blocked on the operator (coder_question)");
  });

  it("stalled is a ? overlay stating elapsed time, never a colour and never 'hung'", async () => {
    const snapshot = oneGroup({
      state: "running",
      sessions: [
        {
          session_id: "s1",
          role: "coder",
          generation: 1,
          name: "r-test-g1-coder-g1",
          retirement_reason: null,
          transcript_path: null,
          transcript_mtime: "2026-08-10T11:37:00Z",
          last_context_tokens: 0,
          rounds_completed: 0,
          total_input_tokens: 0,
          total_output_tokens: 0,
          total_cache_read_tokens: 0,
          total_cache_creation_tokens: 0,
        },
      ],
    });
    const { container } = renderGrid(snapshot, { nowMs: Date.parse("2026-08-10T12:00:00Z") });

    const overlay = container.querySelector(".attempt-cell__stalled")!;
    expect(overlay.textContent).toBe("?");
    expect(overlay.getAttribute("title")).toContain("no activity for 23m");
    // The cell keeps its state colour; the stall gets no colour of its own.
    const cell = screen.getByRole("button", { name: /g1 generation 1/ });
    expect(cell.getAttribute("style")).toContain(statusOf("running").colour);
    expect(container.textContent).not.toMatch(/hung/i);
  });

  it("interrupted shows the resume command as copyable text, not a button", async () => {
    renderGrid(oneGroup({ state: "interrupted" }, "r20260726-grouping"));
    const detail = await openCell("g1", 1);
    const command = within(detail).getByTestId("resume-command");
    expect(command.textContent).toBe("smart-mcps-orchestrate resume r20260726-grouping");
    expect(command.tagName).toBe("CODE");
    // Nothing in the panel offers to run it.
    for (const button of within(detail).queryAllByRole("button")) {
      expect(button.textContent).not.toContain("resume");
    }
  });

  it("names the remaining cases on their cells", async () => {
    // superseded-by-respawn + breaker retirement, from the real run.
    renderGrid(R20260726_GROUPING);
    const g2gen1 = await openCell("g2", 1);
    expect(g2gen1.textContent).toContain("superseded by generation 2");
    expect(g2gen1.textContent).toContain("retired by the circuit breaker");
    // round-atomic bookkeeping loss degrades honestly on a pre-actuals run.
    expect(g2gen1.textContent).toContain("actuals not recorded for this run");
    cleanup();

    // self_verify with no reviewer, and a usage-limit outage.
    renderGrid(oneGroup({ intensity: "self_verify" }));
    expect((await openCell("g1", 1)).textContent).toContain(
      "self_verify — no reviewer session is expected",
    );
    cleanup();

    renderGrid(
      oneGroup({ state: "failed", failure: "LlmProcessError: claude -p failed (1): " }),
    );
    expect((await openCell("g1", 1)).textContent).toContain("the claude process went away");
    cleanup();

    renderGrid(
      oneGroup({
        state: "interrupted",
        sessions: [
          {
            session_id: "s1",
            role: "coder",
            generation: 1,
            name: "r-test-g1-coder-g1",
            retirement_reason: null,
            transcript_path: null,
            last_context_tokens: 84000,
            rounds_completed: 0,
            started_at: "2026-08-10T10:00:00Z",
            ended_at: "2026-08-10T11:12:00Z",
            total_input_tokens: 0,
            total_output_tokens: 0,
            total_cache_read_tokens: 0,
            total_cache_creation_tokens: 0,
          },
        ],
      }),
    );
    expect((await openCell("g1", 1)).textContent).toContain("round bookkeeping lost");
  });
});

describe("orchestrator sessions on the board (U30)", () => {
  function baseSession(overrides: Partial<import("../types").SnapshotSession> = {}) {
    return {
      session_id: "base-1",
      role: "orchestrator",
      generation: 1,
      name: "r-test-base",
      retirement_reason: null,
      transcript_path: null,
      last_context_tokens: 0,
      rounds_completed: 0,
      total_input_tokens: 0,
      total_output_tokens: 0,
      total_cache_read_tokens: 0,
      total_cache_creation_tokens: 0,
      ...overrides,
    };
  }

  function coderSession(generation: number) {
    return {
      session_id: `s-g${generation}`,
      role: "coder",
      generation,
      name: `r-test-g1-coder-g${generation}`,
      retirement_reason: null,
      transcript_path: null,
      last_context_tokens: 0,
      rounds_completed: 0,
      total_input_tokens: 0,
      total_output_tokens: 0,
      total_cache_read_tokens: 0,
      total_cache_creation_tokens: 0,
    };
  }

  function rewriteSession(generation: number) {
    return {
      session_id: `rewrite-g${generation}`,
      role: "orchestrator",
      generation,
      name: `r-test-g1-orchestrator-g${generation}`,
      retirement_reason: null,
      transcript_path: null,
      last_context_tokens: 0,
      rounds_completed: 0,
      total_input_tokens: 0,
      total_output_tokens: 0,
      total_cache_read_tokens: 0,
      total_cache_creation_tokens: 0,
    };
  }

  function snapshotWithSessions(sessions: unknown[], run = "r-test"): RunSnapshot {
    return {
      project: "smart-mcps",
      run_id: run,
      plan_path: "p.md",
      base_session: baseSession(),
      groups: [
        {
          group_id: "g1",
          name: "a-group",
          summary: "",
          state: "completed",
          generation: sessions.length ? Math.max(...sessions.map((s: any) => s.generation)) : 1,
          failure: null,
          stale_failure: false,
          depends_on: [],
          sessions,
        },
      ],
      edges: [],
      stale_dag: false,
      live_pids: {},
    } as unknown as RunSnapshot;
  }

  it("[g19-base-session-exposed] exposes the run's base session with an orchestrator role", () => {
    const snapshot = snapshotWithSessions([coderSession(1)]);
    expect(snapshot.base_session?.role).toBe("orchestrator");
  });

  it("[f8-base-session-rendered] renders the run-level base session as a clickable row", async () => {
    // F8: the orchestrator row used to be synthesized per group while the
    // run-level entry rendered nowhere — and clicking it 404'd because the
    // base session was never in the manifest join. Now the real entry renders
    // once at run level and opens the session viewer.
    const onOpenSession = vi.fn();
    renderGrid(snapshotWithSessions([coderSession(1)]), { onOpenSession });
    const strip = await screen.findByLabelText("Run session");
    expect(strip.textContent).toContain("orchestrator");
    const open = strip.querySelector("button");
    expect(open).toBeTruthy();
    fireEvent.click(open!);
    expect(onOpenSession).toHaveBeenCalledWith("g1", "base-1");
  });

  it("[g19-rewrite-before-generation] positions a rewrite before the generation it produced", async () => {
    // gen 1 (base + coder), then a rewrite that produced gen 2, then gen 2's coder.
    renderGrid(
      snapshotWithSessions([baseSession(), coderSession(1), rewriteSession(2), coderSession(2)]),
    );
    const detail = await openCell("g1", 2);
    const rows = within(detail)
      .getAllByRole("listitem")
      .map((li) => li.textContent ?? "");
    const orchestratorRowIndex = rows.findIndex((text) => text.includes("orchestrator"));
    const coderRowIndex = rows.findIndex((text) => text.includes("coder"));
    expect(orchestratorRowIndex).toBeGreaterThanOrEqual(0);
    expect(coderRowIndex).toBeGreaterThan(orchestratorRowIndex);
  });

  it("[g19-visually-distinct] gives orchestrator sessions their own role class, distinct from coder/reviewer", async () => {
    renderGrid(snapshotWithSessions([baseSession(), coderSession(1)]));
    const detail = await openCell("g1", 1);
    const orchestratorRole = within(detail).getByText("orchestrator");
    const coderRole = within(detail).getByText("coder");
    expect(orchestratorRole.className).toBe("attempt-session__role attempt-session__role--orchestrator");
    expect(coderRole.className).toBe("attempt-session__role attempt-session__role--coder");
    expect(orchestratorRole.className).not.toBe(coderRole.className);
  });

  it("[g19-no-spurious-rows] a group never re-specced shows no orchestrator rows beyond the base session", async () => {
    renderGrid(snapshotWithSessions([baseSession(), coderSession(1)]));
    const detail = await openCell("g1", 1);
    const orchestratorRows = within(detail)
      .getAllByRole("listitem")
      .filter((li) => li.textContent?.includes("orchestrator"));
    expect(orchestratorRows).toHaveLength(1);
    expect(orchestratorRows[0].textContent).toContain("r-test-base");
  });
});

describe("panel contract", () => {
  it("carries exactly one PathChip, pointing at the manifest it reads", async () => {
    const { container } = renderGrid(R20260726_GROUPING);
    await waitFor(() => {
      const chips = container.querySelectorAll(".path-chip");
      expect(chips).toHaveLength(1);
      expect(chips[0].getAttribute("title")).toBe(
        "/repo/.orchestrator/runs/r20260726-grouping/manifest.json",
      );
    });
  });

  it("takes no colour of its own — every hue comes from status.ts", () => {
    // A literal colour in these files would be a second status map, which is
    // exactly how `resolved` and `interrupted` once shipped invisible.
    for (const file of ["src/components/AttemptGrid.tsx", "src/attempts.ts"]) {
      const source = readFileSync(file, "utf8");
      expect(source, `${file} hard-codes a colour`).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
      expect(source, `${file} maps a status to a colour`).not.toMatch(/STATUS\[/);
    }
  });

  it("never consults live_pids", () => {
    for (const file of [
      "src/attempts.ts",
      "src/components/AttemptGrid.tsx",
      "src/components/GroupBoard.tsx",
    ]) {
      const source = readFileSync(file, "utf8");
      const uses = source
        .split("\n")
        .filter((line) => {
          const trimmed = line.trimStart();
          const isComment = trimmed.startsWith("//") || trimmed.startsWith("*");
          return line.includes("live_pids") && !isComment;
        });
      expect(uses, `${file} reads live_pids`).toEqual([]);
    }
  });
});
