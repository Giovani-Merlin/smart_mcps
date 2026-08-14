// The launch surface, mounted from the app's own route table.
//
// What is under test is the thing a form can get wrong invisibly: which options
// reach the request body. A tier chosen in the resume form and then not sent is
// exactly the failure this whole page exists to prevent, and it looks like a
// successful launch from the browser.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { routes } from "../routes";

const startGroupJob = vi.fn();
const startRunJob = vi.fn();
const startResumeJob = vi.fn();

vi.mock("../api", () => ({
  listProjects: () => Promise.resolve([]),
  listRuns: () => Promise.resolve([{ run_id: "r1", updated_at: "2026-08-13T10:00:00Z" }]),
  listPlans: () =>
    Promise.resolve([
      { path: "docs/plans/one.md", title: "The First Plan", modified_at: "2026-08-13T09:00:00Z" },
    ]),
  listGroupings: () =>
    Promise.resolve([{ name: "mine", plan_path: "docs/plans/one.md", group_count: 4 }]),
  startGroupJob: (...args: unknown[]) => startGroupJob(...args),
  startRunJob: (...args: unknown[]) => startRunJob(...args),
  startResumeJob: (...args: unknown[]) => startResumeJob(...args),
  openJobStream: () => ({ close: () => {} }),
  getSnapshot: () => Promise.resolve(null),
  openRunStream: () => ({ close: () => {} }),
  openLogStream: () => ({ close: () => {} }),
  getRunPaths: () => Promise.resolve({ project: "p", run_id: "r", roots: {}, entries: [] }),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
  ApiError: class ApiError extends Error {},
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
    const check = screen.getAllByLabelText("Wait out usage limits")[1];
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
});
