// Plan U29: DiffView renders a DiffResult the way git would — per-file
// headers, +/- coloring, the backend's own "unavailable" reason surfaced
// verbatim, and a truncated diff stated as truncated rather than silently cut.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import DiffView from "./DiffView";
import type { DiffResult } from "../types";

afterEach(cleanup);

const SAMPLE_DIFF = [
  "diff --git a/one.txt b/one.txt",
  "index 0000000..1111111 100644",
  "--- a/one.txt",
  "+++ b/one.txt",
  "@@ -1 +1,2 @@",
  " unchanged",
  "+added line",
  "-removed line",
  "diff --git a/two.txt b/two.txt",
  "index 0000000..2222222 100644",
  "--- a/two.txt",
  "+++ b/two.txt",
  "@@ -0,0 +1 @@",
  "+second file",
].join("\n");

function available(overrides: Partial<DiffResult> = {}): DiffResult {
  return {
    available: true,
    diff: SAMPLE_DIFF,
    truncated: false,
    from_ref: "abc123",
    to_ref: "def456",
    ...overrides,
  };
}

describe("DiffView", () => {
  it("shows a loading state while result is null", () => {
    render(<DiffView title="Group diff" result={null} />);
    expect(screen.getByText("Loading diff…")).toBeTruthy();
  });

  it("surfaces a request error distinctly from an unavailable result", () => {
    render(<DiffView title="Group diff" result={null} error="network down" />);
    expect(screen.getByText("network down")).toBeTruthy();
  });

  it("renders the backend's reason for an unavailable diff rather than an error", () => {
    render(
      <DiffView
        title="Group diff"
        result={{
          available: false,
          reason: "branch 'orchestrator/r1-g1' no longer exists — its worktree was torn down after merging",
          diff: "",
          truncated: false,
        }}
      />,
    );
    expect(screen.getByText(/torn down after merging/)).toBeTruthy();
  });

  it("renders one header per file and does not merge their hunks", () => {
    render(<DiffView title="Group diff" result={available()} />);
    expect(screen.getByText("one.txt")).toBeTruthy();
    expect(screen.getByText("two.txt")).toBeTruthy();
    // Each file's content lives under its own header — findable structurally,
    // not just as flat text.
    const headers = document.querySelectorAll(".diff-view__file-header");
    expect(headers).toHaveLength(2);
    const files = document.querySelectorAll(".diff-view__file");
    expect(files).toHaveLength(2);
  });

  it("color-codes added and removed lines distinctly", () => {
    render(<DiffView title="Group diff" result={available()} />);
    const added = document.querySelector(".diff-view__line--add");
    const removed = document.querySelector(".diff-view__line--del");
    expect(added?.textContent).toContain("+added line");
    expect(removed?.textContent).toContain("-removed line");
  });

  it("states a truncation rather than silently sending a partial diff", () => {
    render(
      <DiffView
        title="Group diff"
        result={available({ truncated: true, total_bytes: 500_000, diff: "x".repeat(200_000) })}
      />,
    );
    expect(screen.getByText(/Truncated/)).toBeTruthy();
  });

  it("renders no truncation notice for a diff under the threshold", () => {
    render(<DiffView title="Group diff" result={available()} />);
    expect(screen.queryByText(/Truncated/)).toBeNull();
  });

  it("reports no changes for an available but empty diff", () => {
    render(<DiffView title="Group diff" result={available({ diff: "" })} />);
    expect(screen.getByText("No changes.")).toBeTruthy();
  });

  it("omits the heading when no title is given", () => {
    render(<DiffView result={available()} />);
    expect(document.querySelector(".diff-view h4")).toBeNull();
  });
});
