// Plan U31: the grouping tab shows the LLM half of a grouping — one row per
// recorded speccer/mapper call, opening the same session viewer used for coder
// and reviewer transcripts, degrading honestly when the `llm/` directory is
// absent, and telling a rewrite call from a grouping-time one by when it ran.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { GroupingView, LlmCallDetail, LlmCallRecord, LlmCallsView } from "../../types";
import { GroupingTab } from "./GroupingTab";

const getGrouping = vi.fn<() => Promise<GroupingView>>();
const getLlmCalls = vi.fn<() => Promise<LlmCallsView>>();
const getLlmCall = vi.fn<() => Promise<LlmCallDetail>>();
const getArtifacts = vi.fn();
const getTranscript = vi.fn();
const getGroupDiff = vi.fn();
const getGenerationDiff = vi.fn();

vi.mock("../../api", () => ({
  getGrouping: (...args: unknown[]) => getGrouping(...(args as [])),
  getLlmCalls: (...args: unknown[]) => getLlmCalls(...(args as [])),
  getLlmCall: (...args: unknown[]) => getLlmCall(...(args as [])),
  getArtifacts: (...args: unknown[]) => getArtifacts(...(args as [])),
  getTranscript: (...args: unknown[]) => getTranscript(...(args as [])),
  getGroupDiff: (...args: unknown[]) => getGroupDiff(...(args as [])),
  getGenerationDiff: (...args: unknown[]) => getGenerationDiff(...(args as [])),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

function baseView(overrides: Partial<GroupingView> = {}): GroupingView {
  return {
    project: "proj",
    run_id: "r1",
    plan_path: "plan.md",
    dag_source: { kind: "run_snapshot", stale_dag: false, reason: "frozen snapshot" },
    missing: [],
    trace_schema_known: true,
    node_work: [],
    config: {},
    hub_roles: [],
    slice_atoms: [],
    stages: [],
    louvain: [],
    splits: [],
    merges: [],
    repairs: [],
    group_difficulty: [],
    flags: [],
    mapper_flags: [],
    partition_flags: [],
    stage_diffs: [],
    paths: {},
    ...overrides,
  };
}

function call(overrides: Partial<LlmCallRecord> = {}): LlmCallRecord {
  return {
    seq: 1,
    recorded_at: "2026-08-20T21:00:00Z",
    "gen_ai.operation.name": "speccer",
    "gen_ai.request.model": "claude-opus-5",
    attempt: 1,
    status: { code: "ok" },
    "gen_ai.usage.input_tokens": 100,
    "gen_ai.usage.output_tokens": 20,
    "claude.usage.cache_read_tokens": 5,
    "claude.usage.cache_creation_tokens": 3,
    ...overrides,
  };
}

function renderTab() {
  render(
    <GroupingTab
      project="proj"
      runId="r1"
      params={new URLSearchParams()}
      onParamsChange={() => {}}
    />,
  );
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("GroupingTab speccer runs", () => {
  it("lists one row per recorded call, with model and token usage", async () => {
    getGrouping.mockResolvedValue(baseView());
    getLlmCalls.mockResolvedValue({
      run_id: "r1",
      directory: "/tmp/llm",
      index_path: "/tmp/llm/calls.json",
      present: true,
      produced_group_ids: [],
      produced_task_ids: [],
      missing: [],
      calls: [
        call({ seq: 1, "gen_ai.operation.name": "mapper" }),
        call({
          seq: 2,
          "gen_ai.operation.name": "speccer",
          "gen_ai.request.model": "claude-sonnet-5",
          "gen_ai.usage.input_tokens": 200,
          "gen_ai.usage.output_tokens": 40,
        }),
      ],
    });

    renderTab();

    await waitFor(() => expect(screen.getByText("mapper")).toBeTruthy());
    expect(screen.getByText("speccer")).toBeTruthy();
    expect(screen.getByText("claude-opus-5")).toBeTruthy();
    expect(screen.getByText("claude-sonnet-5")).toBeTruthy();
    expect(screen.getByText("100/20")).toBeTruthy();
    expect(screen.getByText("200/40")).toBeTruthy();
  });

  it("opens a call's prompt and response in the session viewer", async () => {
    getGrouping.mockResolvedValue(baseView());
    getLlmCalls.mockResolvedValue({
      run_id: "r1",
      directory: "/tmp/llm",
      index_path: "/tmp/llm/calls.json",
      present: true,
      produced_group_ids: [],
      produced_task_ids: [],
      missing: [],
      calls: [call({ seq: 1 })],
    });
    getLlmCall.mockResolvedValue({
      seq: 1,
      call: call({ seq: 1 }),
      request_text: "map these tasks into groups",
      raw_text: '{"groups": []}',
      missing: [],
    });

    renderTab();

    await waitFor(() => expect(screen.getByText("speccer")).toBeTruthy());
    const rows = screen.getAllByRole("button", { pressed: false });
    const timeButton = rows.find((btn) => btn.textContent?.includes("2026"));
    expect(timeButton).toBeTruthy();
    fireEvent.click(timeButton!);

    await waitFor(() =>
      expect(screen.getByText("map these tasks into groups")).toBeTruthy(),
    );
    expect(screen.getByText('{"groups": []}')).toBeTruthy();
    expect(getLlmCall).toHaveBeenCalledWith("proj", "r1", 1);
  });

  it("renders an explanatory empty state when the llm/ directory is absent", async () => {
    getGrouping.mockResolvedValue(baseView());
    getLlmCalls.mockResolvedValue({
      run_id: "r1",
      directory: "/tmp/r1/llm",
      index_path: "/tmp/r1/llm/calls.json",
      present: false,
      produced_group_ids: [],
      produced_task_ids: [],
      missing: [
        {
          artifact: "llm/calls.json",
          expected_path: "/tmp/r1/llm/calls.json",
          explanation: "this grouping recorded no LLM calls.",
        },
      ],
      calls: [],
    });

    renderTab();

    await waitFor(() =>
      expect(screen.getByText("this grouping recorded no LLM calls.")).toBeTruthy(),
    );
    // The rest of the tab (partition-driven sections) still renders — the
    // absence degrades only the LLM section, not the whole tab.
    expect(screen.getByText("Grouping")).toBeTruthy();
  });

  it("shows a rewrite call recorded later alongside the grouping-time calls, distinguishable by when they ran", async () => {
    getGrouping.mockResolvedValue(baseView());
    getLlmCalls.mockResolvedValue({
      run_id: "r1",
      directory: "/tmp/llm",
      index_path: "/tmp/llm/calls.json",
      present: true,
      produced_group_ids: [],
      produced_task_ids: [],
      missing: [],
      calls: [
        call({ seq: 1, recorded_at: "2026-08-20T21:00:00Z" }),
        call({ seq: 2, recorded_at: "2026-08-20T23:45:12Z" }),
      ],
    });

    renderTab();

    await waitFor(() => expect(screen.getAllByText("speccer")).toHaveLength(2));
    const timeButtons = screen
      .getAllByRole("button", { pressed: false })
      .filter((btn) => btn.textContent?.includes("2026"));
    expect(timeButtons).toHaveLength(2);
    expect(timeButtons[0].textContent).not.toEqual(timeButtons[1].textContent);
  });
});
