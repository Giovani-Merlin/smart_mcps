// Plan U28/U35: why one group in thirteen got a second reviewer opinion, and
// why "-g3" on a session name is a generation, not a group reference.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Artifact, DiffResult, RunSnapshot, TranscriptEvent } from "../types";
import GroupDrillIn from "./GroupDrillIn";

const getArtifacts = vi.fn<() => Promise<Artifact[]>>();
const getTranscript = vi.fn<() => Promise<TranscriptEvent[]>>();
const getGroupDiff = vi.fn<() => Promise<DiffResult>>();
const getGenerationDiff = vi.fn<() => Promise<DiffResult>>();

const emptyDiff: DiffResult = {
  available: true,
  diff: "",
  truncated: false,
};

vi.mock("../api", () => ({
  getArtifacts: (...args: unknown[]) => getArtifacts(...(args as [])),
  getTranscript: (...args: unknown[]) => getTranscript(...(args as [])),
  getGroupDiff: (...args: unknown[]) => getGroupDiff(...(args as [])),
  getGenerationDiff: (...args: unknown[]) => getGenerationDiff(...(args as [])),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

function snapshot(overrides: Partial<RunSnapshot["groups"][number]> = {}): RunSnapshot {
  return {
    project: "smart-mcps",
    run_id: "r1",
    plan_path: "plan.md",
    groups: [
      {
        group_id: "g1",
        name: "drill-in",
        summary: "",
        state: "completed",
        generation: 1,
        failure: null,
        stale_failure: false,
        depends_on: [],
        sessions: [
          {
            session_id: "s-coder",
            role: "coder",
            generation: 3,
            name: "r1-g1-coder-g3",
            retirement_reason: null,
            transcript_path: null,
            last_context_tokens: 0,
            rounds_completed: 0,
            total_input_tokens: 0,
            total_output_tokens: 0,
            total_cache_read_tokens: 0,
            total_cache_creation_tokens: 0,
            started_at: "2026-08-20T21:31:34Z",
          },
          {
            session_id: "s-base",
            role: "base",
            generation: 1,
            name: "r1-base",
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
        difficulty: 0.82,
        intensity: "paired_plus",
        pending_surprises: [],
        emitted_surprises: [],
        ...overrides,
      },
    ],
    edges: [],
    stale_dag: false,
    live_pids: {},
  };
}

function verdictArtifact(name: string, isExtra: boolean): Artifact {
  return {
    name,
    kind: "verdict",
    content: { status: "approved", notes: "looks good" },
    error: null,
    denial_kind: null,
    is_extra: isExtra,
  };
}

beforeEach(() => {
  getArtifacts.mockResolvedValue([]);
  getTranscript.mockResolvedValue([]);
  getGroupDiff.mockResolvedValue(emptyDiff);
  getGenerationDiff.mockResolvedValue(emptyDiff);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function openGroup(snap: RunSnapshot): Promise<void> {
  render(<GroupDrillIn project="smart-mcps" runId={snap.run_id} snapshot={snap} revision={1} />);
  fireEvent.click(screen.getByText("g1"));
  await screen.findByText("Sessions");
}

describe("difficulty and intensity (U28)", () => {
  it("shows the group's difficulty score and intensity tier", async () => {
    await openGroup(snapshot());
    expect(screen.getByText("difficulty 0.82")).toBeTruthy();
    expect(screen.getByText("paired_plus")).toBeTruthy();
  });

  it("labels a paired_plus group's -extra verdict as the mandatory second pass", async () => {
    getArtifacts.mockResolvedValue([
      verdictArtifact("verdict-g1-r1.json", false),
      verdictArtifact("verdict-g1-r1-extra.json", true),
    ]);
    await openGroup(snapshot());
    await screen.findByText("verdict-g1-r1-extra.json");

    expect(screen.getByText("extra pass")).toBeTruthy();
    // Both verdicts render in the same viewer (ArtifactCard/VerdictBody) and
    // are distinguishable only by that label.
    expect(screen.getAllByText("approved")).toHaveLength(2);
  });

  it("renders no extra-pass label for a group with no -extra verdict", async () => {
    getArtifacts.mockResolvedValue([verdictArtifact("verdict-g1-r1.json", false)]);
    await openGroup(snapshot());
    await screen.findByText("verdict-g1-r1.json");

    expect(screen.queryByText("extra pass")).toBeNull();
  });
});

describe("session generation naming and timestamps (U35)", () => {
  it("labels a session id ending -coder-g3 as gen 3, not part of the name", async () => {
    await openGroup(snapshot());
    expect(screen.getByText("gen 3")).toBeTruthy();
    expect(screen.queryByText("r1-g1-coder-g3")).toBeNull();
    expect(screen.getByText("r1-g1-coder")).toBeTruthy();
  });

  it("renders no generation label for a session with no generation suffix", async () => {
    await openGroup(snapshot());
    // The base session's name has no trailing "-g<N>" and must not read "gen 0".
    expect(screen.getByText("r1-base")).toBeTruthy();
    expect(screen.queryByText("gen 0")).toBeNull();
  });

  it("shows a session timestamp in the operator's local zone, named", async () => {
    await openGroup(snapshot());
    const zone = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" })
      .formatToParts(new Date("2026-08-20T21:31:34Z"))
      .find((part) => part.type === "timeZoneName")?.value;
    expect(zone).toBeTruthy();
    expect(screen.getByTitle("started at").textContent).toContain(zone);
  });
});

describe("surprise board — pending vs. emitted (U12)", () => {
  it("renders no surprise section when both directions are empty", async () => {
    await openGroup(snapshot());
    expect(screen.queryByText("Pending for this group")).toBeNull();
    expect(screen.queryByText("Emitted by this group")).toBeNull();
  });

  it("shows surprises pending for the group with their reason, separately from what it emitted", async () => {
    await openGroup(
      snapshot({
        pending_surprises: [
          {
            kind: "other",
            description: "late finding from g3",
            affected_groups: [],
            reason: "run ended before delivery",
          },
        ],
        emitted_surprises: [
          { kind: "interface_mismatch", description: "g1 changed the API", affected_groups: ["g2"] },
        ],
      }),
    );

    expect(screen.getByText("Pending for this group")).toBeTruthy();
    expect(screen.getByText(/late finding from g3/)).toBeTruthy();
    expect(screen.getByText("run ended before delivery")).toBeTruthy();

    expect(screen.getByText("Emitted by this group")).toBeTruthy();
    expect(screen.getByText(/g1 changed the API/)).toBeTruthy();
    expect(screen.getByText(/affects g2/)).toBeTruthy();
  });
});
