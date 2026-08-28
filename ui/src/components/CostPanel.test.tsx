// The rendered cost tab, against a real historical run and a synthetic
// new-format one.
//
// The derivation can be perfectly right and the panel still be wrong — a bar
// that lets cache reads dominate, a ratio drawn between an estimate and a
// cumulative total, a self_verify group's missing reviewer painted as an
// absence. Those are the failures these tests are for.

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TOKEN_CLASSES } from "../cost";
import { COST_NEW_FORMAT } from "../fixtures/cost-new-format";
import { R20260726_GROUPING } from "../fixtures/r20260726-grouping";
import type { RunSnapshot } from "../types";
import CostPanel from "./CostPanel";

const getRunPaths = vi.fn();

vi.mock("../api", () => ({
  getRunPaths: (...args: unknown[]) => getRunPaths(...(args as [])),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

const MANIFEST_PATH = "/repo/.orchestrator/runs/r20260726-grouping/manifest.json";

beforeEach(() => {
  getRunPaths.mockResolvedValue({
    project: "smart-mcps",
    run_id: "r20260726-grouping",
    roots: {},
    entries: [
      {
        key: "manifest",
        label: "manifest.json",
        panel: "cost",
        path: MANIFEST_PATH,
        kind: "file",
        exists: true,
      },
      {
        key: "groups",
        label: "groups.json",
        panel: "cost",
        path: "/repo/.orchestrator/runs/r20260726-grouping/groups.json",
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

function renderPanel(snapshot: RunSnapshot) {
  return render(<CostPanel project="smart-mcps" runId={snapshot.run_id} snapshot={snapshot} />);
}

function segment(testId: string, cls: string): HTMLElement {
  return screen.getByTestId(`${testId}-${cls}`);
}

describe("the two quantities live in two panels", () => {
  it("renders prediction and cumulative spend as separate sections", () => {
    renderPanel(COST_NEW_FORMAT);
    const prediction = screen.getByTestId("cost-prediction");
    const spend = screen.getByTestId("cost-spend");

    expect(prediction.contains(spend)).toBe(false);
    expect(spend.contains(prediction)).toBe(false);
    expect(within(prediction).getByText("Prediction vs outcome")).toBeTruthy();
    expect(within(spend).getByText("Cumulative spend")).toBeTruthy();
    // The spend panel never names the estimate, so nothing there can be read
    // as a fraction of it.
    expect(spend.textContent).not.toContain("estimated_tokens");
    expect(spend.textContent).toContain("not comparable with the estimate");
  });

  it("compares the estimate only against the coder's context occupancy", () => {
    renderPanel(COST_NEW_FORMAT);
    const row = screen.getByTestId("cost-prediction-g1");
    // 83.2k predicted, 96.4k observed — occupancy on both sides. g1's
    // cumulative spend is 732k, and that number must not appear in this row.
    expect(row.textContent).toContain("83k");
    expect(row.textContent).toContain("96k");
    expect(row.textContent).not.toContain("732k");
    expect(row.textContent).toContain("1.16x over the estimate");
  });
});

describe("per-group bars are four classes split by role", () => {
  it("draws a coder bar and a reviewer bar, each with all four classes", () => {
    renderPanel(COST_NEW_FORMAT);
    const group = screen.getByTestId("cost-group-g1");
    expect(within(group).getByTestId("cost-role-g1-coder")).toBeTruthy();
    expect(within(group).getByTestId("cost-role-g1-reviewer")).toBeTruthy();

    const expected: Record<string, [number, number]> = {
      // class → [coder tokens, reviewer tokens]
      uncached_input: [12_000, 5_000],
      cache_creation: [60_000, 22_000],
      cache_read: [480_000, 120_000],
      output: [24_000, 9_000],
    };
    for (const cls of TOKEN_CLASSES) {
      const [coder, reviewer] = expected[cls.key];
      expect(segment("cost-role-bar-g1-coder", cls.key).dataset.tokens).toBe(String(coder));
      expect(segment("cost-role-bar-g1-reviewer", cls.key).dataset.tokens).toBe(String(reviewer));
    }
  });

  it("draws the per-round sparkline when the manifest carried rounds", () => {
    renderPanel(COST_NEW_FORMAT);
    const spark = within(screen.getByTestId("cost-group-g1")).getAllByTestId("cost-spark");
    expect(spark.length).toBe(1); // only the coder session has a rounds list
    expect(spark[0].getAttribute("title")).toContain("145,000");
  });
});

describe("cache reads are visually subordinate", () => {
  it("marks only the cache-read segment as muted, in the bars and the legend", () => {
    renderPanel(COST_NEW_FORMAT);
    for (const cls of TOKEN_CLASSES) {
      const seg = segment("cost-role-bar-g1-coder", cls.key);
      expect(seg.dataset.emphasis).toBe(cls.key === "cache_read" ? "muted" : "primary");
      expect(seg.className).toContain(
        cls.key === "cache_read" ? "cost-bar__seg--muted" : "cost-bar__seg--primary",
      );
    }
    expect(screen.getAllByText(/\(cheap\)/).length).toBeGreaterThan(0);
  });

  it("states each role's total excluding cache reads, so the cheap class cannot inflate it", () => {
    renderPanel(COST_NEW_FORMAT);
    // Coder: 576k in total, 96k once the cache reads are set aside.
    expect(screen.getByTestId("cost-role-total-g1-coder").textContent).toContain(
      "96k excluding cache reads",
    );
  });
});

describe("intensity and the expected reviewer count are stated", () => {
  it("reads a self_verify group's zero reviewer sessions as expected", () => {
    renderPanel(COST_NEW_FORMAT);
    const note = screen.getByTestId("cost-expectation-g2");
    expect(note.textContent).toContain("self_verify: 0 reviewer sessions expected");
    expect(note.textContent).toContain("correct for this group, not missing data");
    expect(note.className).toContain("cost-group__expectation--ok");
    expect(screen.getByTestId("cost-intensity-g2").textContent).toBe("self_verify");
  });

  it("says unknown when groups.json carried no intensity", () => {
    renderPanel(COST_NEW_FORMAT);
    expect(screen.getByTestId("cost-intensity-g3").textContent).toBe("intensity unknown");
    const note = screen.getByTestId("cost-expectation-g3");
    expect(note.textContent).toContain("intensity unknown");
    expect(note.textContent).toContain("no recorded intensity to say how many were expected");
    expect(note.className).not.toContain("--ok");
  });

  it("shows paired_plus as one session plus a mandatory extra round", () => {
    renderPanel(COST_NEW_FORMAT);
    expect(screen.getByTestId("cost-expectation-g4").textContent).toContain(
      "1 reviewer session expected, plus one mandatory extra round",
    );
  });
});

describe("the run-level calibration rollup", () => {
  it("summarises predicted against observed occupancy and names the exclusions", () => {
    renderPanel(COST_NEW_FORMAT);
    const rollup = screen.getByTestId("cost-calibration");
    expect(rollup.textContent).toContain("Estimator calibration across the run");
    expect(screen.getByTestId("cost-calibration-median").textContent).toBe(
      "1.16x over the estimate",
    );
    expect(rollup.textContent).toContain("median across 3 single-generation groups");
    expect(rollup.textContent).toContain("bytes_per_token");
    expect(screen.getByTestId("cost-calibration-skipped").textContent).toContain("g4");
    // 219k observed against 213k predicted — both occupancy sums. The run's
    // cumulative spend (1.1M) is not in this panel at all.
    expect(rollup.textContent).toContain("219k observed");
    expect(rollup.textContent).toContain("213k predicted");
  });
});

describe("a real historical run degrades honestly", () => {
  it("says actuals are not recorded and offers the manifest path", async () => {
    renderPanel(R20260726_GROUPING);

    const note = screen.getByTestId("cost-actuals-missing");
    expect(note.textContent).toContain("actuals not recorded for this run");
    expect(note.textContent).toContain("missing bookkeeping, not a run that spent nothing");

    // The PathChip to the manifest — the file the operator opens next.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: `Copy path ${MANIFEST_PATH}` })).toBeTruthy(),
    );
  });

  it("shows no spend bars for a legacy group, only the estimate note", () => {
    renderPanel(R20260726_GROUPING);
    const group = screen.getByTestId("cost-group-g1");
    expect(within(group).queryByTestId("cost-role-g1-coder")).toBeNull();
    expect(within(group).getByTestId("cost-absent-g1").textContent).toContain(
      "actuals not recorded for this run",
    );
  });

  it("still compares nothing dishonestly: no ratio without an occupancy pair", () => {
    renderPanel(R20260726_GROUPING);
    // The fixture's groups carry no `estimated_tokens`, so every row is "not
    // comparable" rather than a fabricated 1.00x.
    expect(screen.getByTestId("cost-prediction-g1").textContent).toContain("not comparable");
    expect(screen.getByTestId("cost-calibration").textContent).toContain("nothing to calibrate");
  });
});

describe("the turn-1 inherited cache read is its own figure (U9)", () => {
  it("shows it distinct from the total cache-read segment", () => {
    renderPanel(COST_NEW_FORMAT);
    const inherited = screen.getByTestId(
      "cost-inherited-cache-c0000000-0000-4000-8000-000000000001",
    );
    expect(inherited.textContent).toContain("90k");
    // The total cache-read segment for the same session is the sum of every
    // turn's cache read (480k), not the 90k inherited figure alone.
    const cacheReadSeg = segment("cost-bar-c0000000-0000-4000-8000-000000000001", "cache_read");
    expect(cacheReadSeg.dataset.tokens).toBe("480000");
  });

  it("renders nothing when no inherited figure was recorded", () => {
    renderPanel(COST_NEW_FORMAT);
    // g2's coder carries no total_inherited_cache_read_tokens in the fixture.
    expect(
      screen.queryByTestId("cost-inherited-cache-c0000000-0000-4000-8000-000000000002"),
    ).toBeNull();
  });
});

describe("spend sums every turn of a round, not just the last (U9)", () => {
  it("renders a cache-creation figure that scales with the round's turn count", () => {
    const threeTurnSnapshot: RunSnapshot = {
      ...COST_NEW_FORMAT,
      run_id: "r-turns",
      groups: [
        {
          group_id: "h1",
          name: "one-turn",
          summary: "single-turn round",
          state: "completed",
          generation: 1,
          failure: null,
          stale_failure: false,
          depends_on: [],
          sessions: [
            {
              session_id: "h0000000-0000-4000-8000-000000000001",
              role: "coder",
              generation: 1,
              name: "h1-coder",
              transcript_path: null,
              last_context_tokens: 10_000,
              rounds_completed: 1,
              total_input_tokens: 0,
              total_output_tokens: 0,
              total_cache_read_tokens: 0,
              // one turn's worth
              total_cache_creation_tokens: 5_000,
              model: "claude-opus-5",
              started_at: null,
              ended_at: null,
            },
          ],
        },
        {
          group_id: "h2",
          name: "three-turns",
          summary: "same per-turn cache-creation, summed over three turns",
          state: "completed",
          generation: 1,
          failure: null,
          stale_failure: false,
          depends_on: [],
          sessions: [
            {
              session_id: "h0000000-0000-4000-8000-000000000002",
              role: "coder",
              generation: 1,
              name: "h2-coder",
              transcript_path: null,
              last_context_tokens: 10_000,
              rounds_completed: 1,
              total_input_tokens: 0,
              total_output_tokens: 0,
              total_cache_read_tokens: 0,
              // three turns' worth of the same per-turn figure, exactly what
              // RoundSpend.from_envelope now sums instead of reading iterations[-1]
              total_cache_creation_tokens: 15_000,
              model: "claude-opus-5",
              started_at: null,
              ended_at: null,
            },
          ],
        },
      ],
    };
    renderPanel(threeTurnSnapshot);
    const oneTurn = segment("cost-role-bar-h1-coder", "cache_creation");
    const threeTurns = segment("cost-role-bar-h2-coder", "cache_creation");
    expect(Number(threeTurns.dataset.tokens)).toBe(3 * Number(oneTurn.dataset.tokens));
  });
});

describe("calibration reports both a last-generation and a peak ratio (U10)", () => {
  it("includes a single-generation group in the median with one ratio", () => {
    renderPanel(COST_NEW_FORMAT);
    const row = screen.getByTestId("cost-calibration-row-g1");
    expect(row.dataset.multiGeneration).toBe("false");
    expect(screen.getByTestId("cost-calibration-generations-g1").textContent).toBe("1");
    expect(screen.queryByTestId("cost-calibration-label-g1")).toBeNull();
  });

  it("shows a multi-generation group's last and peak ratios, labelled with its generation count and retirement reasons, and excludes it from the median", () => {
    renderPanel(COST_NEW_FORMAT);
    const row = screen.getByTestId("cost-calibration-row-g5");
    expect(row.dataset.multiGeneration).toBe("true");
    expect(screen.getByTestId("cost-calibration-generations-g5").textContent).toContain("4");

    const label = screen.getByTestId("cost-calibration-label-g5");
    expect(label.textContent).toContain("multi-generation");
    expect(label.textContent).toContain("excluded from median");
    expect(label.textContent).toContain("breaker retired");
    expect(label.textContent).toContain("merge conflict");
    expect(label.textContent).toContain("re-entry fallback");

    // last generation: 45k / 50k = 0.9x; peak: 80k / 50k = 1.6x.
    expect(row.textContent).toContain("0.90x");
    expect(row.textContent).toContain("1.60x");

    // g5 must not be counted in the 3-single-generation-group median computed
    // from g1/g2/g3 elsewhere in this file.
    const rollup = screen.getByTestId("cost-calibration");
    expect(rollup.textContent).toContain("median across 3 single-generation groups");
    expect(rollup.textContent).toContain("1 multi-generation row excluded");
  });

  it("reports no median, not a median over zero rows, when every group is multi-generation", () => {
    const allMultiGen: RunSnapshot = {
      ...COST_NEW_FORMAT,
      run_id: "r-all-multi-gen",
      groups: COST_NEW_FORMAT.groups.filter((g) => g.group_id === "g5"),
    };
    renderPanel(allMultiGen);
    const median = screen.getByTestId("cost-calibration-median");
    expect(median.textContent).toBe("no median — no single-generation groups to compute one from");
    expect(median.textContent).not.toContain("NaN");
    const rollup = screen.getByTestId("cost-calibration");
    expect(rollup.textContent).toContain("median across 0 single-generation groups");
  });
});
