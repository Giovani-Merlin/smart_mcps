// The launch surface, mounted from the app's own route table.
//
// What is under test is the thing a form can get wrong invisibly: which options
// reach the request body. A tier chosen in the resume form and then not sent is
// exactly the failure this whole page exists to prevent, and it looks like a
// successful launch from the browser.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { routes } from "../routes";

const startGroupJob = vi.fn();
const startRunJob = vi.fn();
const startResumeJob = vi.fn();
const listGroupings = vi.fn();
const getJob = vi.fn();
const listJobs = vi.fn();

const ONE_GROUPING = [{ name: "mine", plan_path: "docs/plans/one.md", group_count: 4 }];

vi.mock("../api", () => ({
  listProjects: () => Promise.resolve([]),
  listRuns: () => Promise.resolve([{ run_id: "r1", updated_at: "2026-08-13T10:00:00Z" }]),
  listPlans: () =>
    Promise.resolve([
      { path: "docs/plans/one.md", title: "The First Plan", modified_at: "2026-08-13T09:00:00Z" },
    ]),
  listGroupings: (...args: unknown[]) => listGroupings(...args),
  listJobs: (...args: unknown[]) => listJobs(...args),
  getJob: (...args: unknown[]) => getJob(...args),
  startGroupJob: (...args: unknown[]) => startGroupJob(...args),
  startRunJob: (...args: unknown[]) => startRunJob(...args),
  startResumeJob: (...args: unknown[]) => startResumeJob(...args),
  openJobStream: () => ({ close: () => {} }),
  getSnapshot: () => Promise.resolve(null),
  openRunStream: () => ({ close: () => {} }),
  openLogStream: () => ({ close: () => {} }),
  getRunPaths: () => Promise.resolve({ project: "p", run_id: "r", roots: {}, entries: [] }),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
  ApiError: class ApiError extends Error {
    constructor(
      readonly status: number,
      message: string,
    ) {
      super(message);
    }
  },
}));

const job = {
  job_id: "j1",
  kind: "run",
  argv: [],
  pid: 1,
  running: true,
  log_path: "/tmp/log",
  options: {},
};

function mount(path = "/p/proj/launch") {
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />);
}

beforeEach(() => {
  startGroupJob.mockReset().mockResolvedValue(job);
  startRunJob.mockReset().mockResolvedValue(job);
  startResumeJob.mockReset().mockResolvedValue(job);
  listGroupings.mockReset().mockResolvedValue(ONE_GROUPING);
  getJob.mockReset().mockResolvedValue(job);
  listJobs.mockReset().mockResolvedValue([]);
});

afterEach(cleanup);

describe("the launch route", () => {
  it("is reachable at /p/:project/launch and offers all three cards", async () => {
    mount();
    expect(await screen.findByRole("region", { name: "Group a plan" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Start a run" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Resume a run" })).toBeTruthy();
  });

  it("posts the plan chosen from the picker", async () => {
    mount();
    const picker = (await screen.findByLabelText("Plan document")) as HTMLSelectElement;
    fireEvent.change(picker, { target: { value: "docs/plans/one.md" } });
    fireEvent.click(screen.getByRole("button", { name: "Group" }));
    await waitFor(() => expect(startGroupJob).toHaveBeenCalled());
    expect(startGroupJob.mock.calls[0][1].plan).toBe("docs/plans/one.md");
  });

  it("accepts a typed path for a plan the picker does not know about", async () => {
    mount();
    const field = await screen.findByLabelText("…or a path, relative to the repo");
    fireEvent.change(field, { target: { value: "elsewhere/other-plan.md" } });
    fireEvent.click(screen.getByRole("button", { name: "Group" }));
    await waitFor(() => expect(startGroupJob).toHaveBeenCalled());
    expect(startGroupJob.mock.calls[0][1].plan).toBe("elsewhere/other-plan.md");
  });

  it("will not group with no plan named at all", async () => {
    mount();
    const button = (await screen.findByRole("button", { name: "Group" })) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  it("sends the escalation tier chosen on the run form", async () => {
    mount();
    fireEvent.change(await screen.findByLabelText("Grouping"), { target: { value: "mine" } });
    fireEvent.click(screen.getAllByLabelText("Human in the loop")[0]);
    fireEvent.change(screen.getAllByLabelText("Escalation tier")[0], {
      target: { value: "on_stuck" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Start run" }));
    await waitFor(() => expect(startRunJob).toHaveBeenCalled());
    const body = startRunJob.mock.calls[0][1];
    expect(body.grouping).toBe("mine");
    expect(body.options.intensity).toBe("on_stuck");
    expect(body.options.hitl).toBe(true);
  });

  it("sends the tier chosen on the resume form — the trap this page exists for", async () => {
    mount();
    fireEvent.change(await screen.findByLabelText("Run"), { target: { value: "r1" } });
    fireEvent.change(screen.getAllByLabelText("Escalation tier")[1], {
      target: { value: "interactive" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(startResumeJob).toHaveBeenCalled());
    expect(startResumeJob.mock.calls[0][1]).toEqual({
      run_id: "r1",
      options: { intensity: "interactive" },
    });
  });

  it("omits an unset option entirely rather than sending a value it invented", async () => {
    mount();
    fireEvent.change(await screen.findByLabelText("Run"), { target: { value: "r1" } });
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(startResumeJob).toHaveBeenCalled());
    // Nothing touched, so nothing is specified: the run keeps what it started
    // with, which is the behaviour the persisted config restores.
    expect(startResumeJob.mock.calls[0][1].options).toEqual({});
  });

  it("unticking 'wait out usage limits' sends the opt-out, ticking sends nothing", async () => {
    mount();
    fireEvent.change(await screen.findByLabelText("Run"), { target: { value: "r1" } });
    // Three cards now carry this label — group, run, resume — so the resume
    // form's is the third.
    const check = screen.getAllByLabelText("Wait out usage limits")[2];
    fireEvent.click(check); // untick → --no-auto-resume
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(startResumeJob).toHaveBeenCalled());
    expect(startResumeJob.mock.calls[0][1].options.auto_resume).toBe(false);
  });

  it("shows the backend's own message when a launch is refused", async () => {
    startResumeJob.mockRejectedValue(new Error("run r1 is already running (pids ['4242'])"));
    mount();
    fireEvent.change(await screen.findByLabelText("Run"), { target: { value: "r1" } });
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    expect(await screen.findByText(/already running/)).toBeTruthy();
  });

  it("streams the launched job's log", async () => {
    mount();
    fireEvent.change(await screen.findByLabelText("Grouping"), { target: { value: "mine" } });
    fireEvent.click(screen.getByRole("button", { name: "Start run" }));
    expect(await screen.findByRole("region", { name: "Job log" })).toBeTruthy();
  });

  it("the project index links to it", async () => {
    mount("/p/proj");
    const link = (await screen.findByRole("link", { name: "New run" })) as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("/p/proj/launch");
  });

  // ---------------------------------------------------------- U21: group options

  it("renders granularity, token budget and auto-resume on the group form", async () => {
    mount();
    expect(await screen.findByLabelText("Granularity")).toBeTruthy();
    expect(screen.getByLabelText("Token budget")).toBeTruthy();
    // Same label the run/resume forms use for the same option; the group
    // form's is the first of the three on the page.
    expect(screen.getAllByLabelText("Wait out usage limits")[0]).toBeTruthy();
  });

  it("posts a body whose granularity is 'balanced' when that is chosen", async () => {
    mount();
    fireEvent.change(await screen.findByLabelText("Plan document"), {
      target: { value: "docs/plans/one.md" },
    });
    fireEvent.change(screen.getByLabelText("Granularity"), { target: { value: "balanced" } });
    fireEvent.click(screen.getByRole("button", { name: "Group" }));
    await waitFor(() => expect(startGroupJob).toHaveBeenCalled());
    expect(startGroupJob.mock.calls[0][1].granularity).toBe("balanced");
  });

  it("omits granularity, token budget and auto-resume when none is touched, and the job still starts", async () => {
    mount();
    fireEvent.change(await screen.findByLabelText("Plan document"), {
      target: { value: "docs/plans/one.md" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Group" }));
    await waitFor(() => expect(startGroupJob).toHaveBeenCalled());
    const body = startGroupJob.mock.calls[0][1];
    expect(body.granularity).toBeFalsy();
    expect(body.token_budget).toBeFalsy();
    expect(body.auto_resume).toBeFalsy();
    // The job still starts: `onLaunched` fired and the log surface appeared.
    expect(await screen.findByRole("region", { name: "Job log" })).toBeTruthy();
  });

  it("shows the resolved options the backend echoed back on the submitted job", async () => {
    startGroupJob.mockResolvedValue({
      ...job,
      kind: "group",
      options: { plan: "docs/plans/one.md", granularity: "balanced", token_budget: 50000 },
    });
    mount();
    fireEvent.change(await screen.findByLabelText("Plan document"), {
      target: { value: "docs/plans/one.md" },
    });
    fireEvent.change(screen.getByLabelText("Granularity"), { target: { value: "balanced" } });
    fireEvent.click(screen.getByRole("button", { name: "Group" }));
    await waitFor(() => expect(startGroupJob).toHaveBeenCalled());
    fireEvent.click(await screen.findByText("Resolved options"));
    expect(await screen.findByText(/"granularity": "balanced"/)).toBeTruthy();
  });

  // ------------------------------------------------------------- U22: live refresh

  it("makes a grouping that appears server-side selectable without a reload", async () => {
    vi.useFakeTimers();
    try {
      listGroupings.mockResolvedValue([]);
      mount();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.queryByRole("option", { name: /mine/ })).toBeNull();

      listGroupings.mockResolvedValue(ONE_GROUPING);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(6000);
      });
      expect(screen.getByRole("option", { name: /mine/ })).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops a finished job's log reporting 'running' without a reload", async () => {
    // Fake timers wrap the whole interaction here rather than just the final
    // advance: an interval created under real timers keeps ticking on the
    // real clock even after `vi.useFakeTimers()` is called later, so the
    // component's own poll has to be *created* under the fake clock for
    // advancing it to have any effect. `waitFor`/`findBy*` are avoided in
    // favour of a manual microtask flush, since their own polling uses
    // `setTimeout` too and would otherwise deadlock against the fake clock.
    vi.useFakeTimers();
    try {
      getJob.mockResolvedValue({ ...job, running: true });
      mount();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      fireEvent.change(screen.getByLabelText("Grouping"), { target: { value: "mine" } });
      fireEvent.click(screen.getByRole("button", { name: "Start run" }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByText("running")).toBeTruthy();

      getJob.mockResolvedValue({ ...job, running: false });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(4000);
      });
      expect(screen.getByText("exited")).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops refetching once the page unmounts", async () => {
    vi.useFakeTimers();
    try {
      mount();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      const callsBeforeUnmount = listGroupings.mock.calls.length;
      cleanup();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(20000);
      });
      expect(listGroupings.mock.calls.length).toBe(callsBeforeUnmount);
    } finally {
      vi.useRealTimers();
    }
  });

  it("leaves the last known data on screen when a refetch fails", async () => {
    vi.useFakeTimers();
    try {
      mount();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByRole("option", { name: /mine/ })).toBeTruthy();

      listGroupings.mockRejectedValue(new Error("network blip"));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(6000);
      });
      // The option is still there — the failed refetch did not blank the form.
      expect(screen.getByRole("option", { name: /mine/ })).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  // --------------------------------------------------------------- U23: job routes

  it("links a launched job to its own addressable page", async () => {
    mount();
    fireEvent.change(await screen.findByLabelText("Grouping"), { target: { value: "mine" } });
    fireEvent.click(screen.getByRole("button", { name: "Start run" }));
    const link = (await screen.findByRole("link", {
      name: "Open this job's own page",
    })) as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("/p/proj/jobs/j1");
  });
});
