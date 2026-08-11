// What the chip shows and what it puts on the clipboard are two different
// strings, and that is the whole point: the display is ellipsised so a header
// stays readable, the clipboard gets the path that actually opens.
//
// A chip that copies what it displays is worse than no chip — the operator
// pastes an ellipsis into a terminal and gets "no such file", which is a bug
// that looks like a typo.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import PathChip, { middleEllipsis } from "./PathChip";
import PathsDrawer from "./PathsDrawer";

const LONG = "/home/op/wksp/smart-mcps/.orchestrator/runs/r20260726-grouping/manifest.json";

const writeText = vi.fn<(text: string) => Promise<void>>();

const getRunPaths = vi.fn();
vi.mock("../api", () => ({
  getRunPaths: (...args: unknown[]) => getRunPaths(...(args as [])),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

beforeEach(() => {
  writeText.mockReset();
  writeText.mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
});

afterEach(cleanup);

describe("PathChip", () => {
  it("copies the full path while displaying a middle-ellipsised one", async () => {
    render(<PathChip path={LONG} label="manifest" />);

    const shown = document.querySelector(".path-chip__path")?.textContent ?? "";
    expect(shown).not.toBe(LONG);
    expect(shown).toContain("…");
    // Both ends survive: the run id and the filename are what identify a path.
    expect(shown.startsWith("/home/op/")).toBe(true);
    expect(shown.endsWith("manifest.json")).toBe(true);

    fireEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(LONG));
    // And says so, so the operator does not click twice wondering.
    await waitFor(() => expect(screen.getByText("copied")).toBeDefined());
  });

  it("leaves a path that already fits alone", () => {
    render(<PathChip path="/repo/state.json" />);
    expect(document.querySelector(".path-chip__path")?.textContent).toBe("/repo/state.json");
  });

  it("keeps the full path reachable without a clipboard, via the title", () => {
    render(<PathChip path={LONG} />);
    expect(screen.getByRole("button").getAttribute("title")).toBe(LONG);
  });

  it("never fetches the file it points at", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(<PathChip path={LONG} />);
    fireEvent.click(screen.getByRole("button"));
    // Display and copy only — exposing a path is zero risk, serving one is not.
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it("middle-ellipsises to exactly the budget it was given", () => {
    expect(middleEllipsis("abcdefghij", 5)).toHaveLength(5);
    expect(middleEllipsis("abcdefghij", 5)).toBe("ab…ij");
  });
});

describe("PathsDrawer", () => {
  const entries = [
    {
      key: "manifest",
      label: "manifest",
      panel: "board",
      path: "/repo/.orchestrator/runs/r1/manifest.json",
      kind: "file",
      exists: true,
    },
    {
      key: "trace",
      label: "trace",
      panel: "grouping",
      path: "/repo/.orchestrator/groupings/g/grouping-trace.json",
      kind: "file",
      exists: false,
    },
  ];

  beforeEach(() => {
    getRunPaths.mockResolvedValue({ project: "p", run_id: "r1", roots: {}, entries });
  });

  it("copies every path at once, one per line", async () => {
    render(<PathsDrawer project="p" runId="r1" />);
    await waitFor(() => expect(screen.getByText("copy all")).toBeDefined());

    fireEvent.click(screen.getByText("copy all"));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(entries.map((e) => e.path).join("\n")),
    );
    await waitFor(() => expect(screen.getByText("2 paths copied")).toBeDefined());
  });

  it("lists an artifact that does not exist rather than hiding it", async () => {
    render(<PathsDrawer project="p" runId="r1" />);
    // The path a missing artifact *would* have had is the one the operator
    // most wants when a panel comes up empty.
    await waitFor(() => expect(screen.getByText("does not exist")).toBeDefined());
  });
});
