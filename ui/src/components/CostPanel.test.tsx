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
    expect(rollup.textContent).toContain("median across 3 groups");
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
