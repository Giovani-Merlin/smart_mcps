// The route shape, mounted from the app's own route table.
//
// These tests exist because the router is the one thing every other surface is
// addressed through: a tab that stops being reachable by URL, or a query param
// that gets dropped on a navigation, breaks every link an operator has ever
// shared and does it silently — the page still renders, just not the one that
// was asked for.
//
// `createMemoryRouter(routes)` rather than a hand-written table: a route test
// that describes its own routes proves the test's routes work.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { routes } from "./routes";

// Every panel under a run fetches; none of that is what is under test here, so
// the whole API module is stubbed to something inert and empty. What is under
// test is which component the URL selects and what the URL still says
// afterwards.
vi.mock("./api", () => ({
  listProjects: () => Promise.resolve([]),
  listRuns: () =>
    Promise.resolve([
      { run_id: "r20260726-grouping", updated_at: "2026-07-26T10:00:00Z" },
      { run_id: "r20260725-older", updated_at: "2026-07-25T10:00:00Z" },
    ]),
  getSnapshot: () => Promise.resolve(null),
  getRunPaths: () =>
    Promise.resolve({ project: "proj", run_id: "run1", roots: {}, entries: [] }),
  getGrouping: () => Promise.reject(new Error("no grouping artifact in this test")),
  listEscalations: () => Promise.resolve([]),
  listArtifacts: () => Promise.resolve([]),
  getTranscript: () => Promise.resolve([]),
  openRunStream: () => ({ close: () => {} }),
  openLogStream: () => ({ close: () => {} }),
  answerEscalation: () => Promise.resolve({}),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
  ApiError: class ApiError extends Error {},
}));

function mount(initial: string) {
  const router = createMemoryRouter(routes, { initialEntries: [initial] });
  render(<RouterProvider router={router} />);
  return router;
}

const RUN = "/p/proj/r/run1";

beforeEach(() => {
  // `EventLog` pins its pane to the newest line; jsdom has no layout.
  Element.prototype.scrollTo ??= () => {};
});

afterEach(cleanup);

describe("run tabs", () => {
  // Each tab is identified by something only that tab renders, so a route
  // silently falling through to another one fails rather than passing.
  const tabs: [string, string, RegExp][] = [
    ["Board", "board", /Groups/],
    ["History", "history", /Attempt history/i],
    ["Grouping", "grouping", /Grouping/],
    ["Escalations", "escalations", /Escalations/],
    ["Log", "log", /Event log/i],
    ["Cost", "cost", /Cost/i],
  ];

  for (const [label, path, marker] of tabs) {
    it(`reaches ${label} at ${RUN}/${path}`, async () => {
      mount(`${RUN}/${path}`);
      await waitFor(() => expect(screen.getAllByText(marker).length).toBeGreaterThan(0));
      // The tab strip marks exactly the current route.
      const current = document.querySelector(".app__tab--current");
      expect(current?.textContent).toBe(label);
    });
  }

  it("sends a bare run URL to the Board rather than nowhere", async () => {
    const router = mount(RUN);
    await waitFor(() => expect(router.state.location.pathname).toBe(`${RUN}/board`));
  });

  it("keeps the session viewer addressable without making it a tab", async () => {
    mount(`${RUN}/session/g2/sess-abc`);
    await waitFor(() => expect(screen.getAllByText(/Group drill-in/i).length).toBeGreaterThan(0));
    // Six tabs, and none of them is the session viewer.
    const labels = [...document.querySelectorAll(".app__tab")].map((el) => el.textContent);
    expect(labels).toEqual(["Board", "History", "Grouping", "Escalations", "Log", "Cost"]);
  });
});

describe("the run index", () => {
  it("lists every run instead of jumping to the newest", async () => {
    const router = mount("/p/proj");
    await waitFor(() => expect(screen.getByText("r20260725-older")).toBeDefined());
    // The dead end this route exists to fix: the older run is reachable, and
    // landing on the index did not redirect anywhere.
    expect(router.state.location.pathname).toBe("/p/proj");
    expect(screen.getByText("r20260726-grouping")).toBeDefined();
  });
});

describe("query params", () => {
  // Path segments identify objects, query params identify view state. A
  // navigation that drops the latter turns a shared link into a different view.
  it("round-trips ?group=, ?stage=, ?edge= and ?seq= through navigation", async () => {
    const query = "?group=g2&stage=merge&edge=t1-t4&seq=17";
    const router = mount(`${RUN}/grouping${query}`);
    await waitFor(() => expect(router.state.location.pathname).toBe(`${RUN}/grouping`));

    const params = new URLSearchParams(router.state.location.search);
    expect(params.get("group")).toBe("g2");
    expect(params.get("stage")).toBe("merge");
    expect(params.get("edge")).toBe("t1-t4");
    expect(params.get("seq")).toBe("17");

    // Navigating within the run keeps them: the router owns the search string
    // and nothing in the shell rebuilds it from scratch.
    await router.navigate({ pathname: `${RUN}/board`, search: query });
    await waitFor(() => expect(router.state.location.pathname).toBe(`${RUN}/board`));
    expect(new URLSearchParams(router.state.location.search).get("group")).toBe("g2");
  });

  it("survives a reload of a deep link — the backend serves index.html for it", async () => {
    // A refresh is a cold mount at the deep URL, which is exactly this. It only
    // works in a browser because `_mount_spa` falls through to `index.html` for
    // any path outside a server-owned prefix; if that route were removed this
    // test would still pass and the app would 404, so the guard for it lives in
    // `test_observatory_api`, not here.
    mount(`${RUN}/grouping?group=g2`);
    await waitFor(() => expect(document.querySelector(".app__tab--current")).toBeTruthy());
    expect(document.querySelector(".app__tab--current")?.textContent).toBe("Grouping");
  });
});

describe("non-goals", () => {
  it("offers no run-control affordance anywhere in the shell", async () => {
    mount(`${RUN}/board`);
    await waitFor(() => expect(document.querySelector(".app__tabs")).toBeTruthy());
    // Launching, resuming and aborting a run are explicit non-goals, and so is
    // editing a plan or config. The check is on the shell's own chrome.
    const chrome = document.querySelector(".app")?.textContent ?? "";
    for (const forbidden of [/\blaunch\b/i, /\bresume\b/i, /\babort\b/i, /\bedit\b/i]) {
      expect(chrome).not.toMatch(forbidden);
    }
  });
});
