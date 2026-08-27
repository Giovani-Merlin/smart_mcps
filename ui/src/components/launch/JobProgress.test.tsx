// A grouping job's only countable stage is `spec i/N` (plan U24) — these tests
// pin the percentage math and, more importantly, the fallback: a job that
// never emits a recognisable line must render nothing rather than a bar stuck
// at 0%, which would look worse than no bar at all.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import JobProgress, { formatElapsed, latestSpecProgress } from "./JobProgress";

afterEach(cleanup);

describe("latestSpecProgress", () => {
  it("finds no progress in an empty or unrecognisable log", () => {
    expect(latestSpecProgress([])).toBeNull();
    expect(latestSpecProgress(["stage: mapper", "stage: graph"])).toBeNull();
  });

  it("reads the count out of a spec line", () => {
    expect(latestSpecProgress(["stage: specs total=4", "spec 2/4"])).toEqual({
      current: 2,
      total: 4,
    });
  });

  it("tracks the most recent spec line, not the first", () => {
    const lines = ["spec 1/4", "spec 2/4", "spec 3/4"];
    expect(latestSpecProgress(lines)).toEqual({ current: 3, total: 4 });
  });

  it("ignores a malformed total of zero", () => {
    expect(latestSpecProgress(["spec 1/0"])).toBeNull();
  });
});

describe("formatElapsed", () => {
  it("renders sub-minute durations as seconds", () => {
    expect(formatElapsed(45_000)).toBe("45s");
  });

  it("renders minutes and zero-padded seconds", () => {
    expect(formatElapsed(3 * 60 * 1000 + 5000)).toBe("3m 05s");
  });

  it("never goes negative", () => {
    expect(formatElapsed(-5000)).toBe("0s");
  });
});

describe("JobProgress", () => {
  it("renders nothing when no spec line has arrived — the fallback for an", () => {
    const { container } = render(
      <JobProgress lines={["stage: mapper", "stage: graph"]} running={true} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing for a job with no lines at all", () => {
    const { container } = render(<JobProgress lines={[]} running={true} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders a determinate bar sized to the latest spec fraction", () => {
    render(<JobProgress lines={["stage: specs total=4", "spec 1/4"]} running={true} />);
    const fill = document.querySelector(".job-progress__fill") as HTMLElement;
    expect(fill.style.width).toBe("25%");
    expect(screen.getByText(/spec 1\/4/)).toBeTruthy();
  });

  it("advances as more spec lines arrive", () => {
    const { rerender } = render(
      <JobProgress lines={["spec 1/4"]} running={true} startedAt={null} />,
    );
    let fill = document.querySelector(".job-progress__fill") as HTMLElement;
    expect(fill.style.width).toBe("25%");

    rerender(<JobProgress lines={["spec 1/4", "spec 2/4"]} running={true} startedAt={null} />);
    fill = document.querySelector(".job-progress__fill") as HTMLElement;
    expect(fill.style.width).toBe("50%");
  });

  it("shows an elapsed time once startedAt is known", () => {
    const startedAt = new Date(Date.now() - 65_000).toISOString();
    render(<JobProgress lines={["spec 1/4"]} running={true} startedAt={startedAt} />);
    expect(screen.getByText(/elapsed/)).toBeTruthy();
  });
});
