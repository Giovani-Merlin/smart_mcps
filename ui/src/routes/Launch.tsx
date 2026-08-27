// `/p/:project/launch` — the surface that starts work.
//
// Three cards, in the order the work happens: group a plan, start a run from a
// grouping, resume a run that stopped. Each posts a job and then hands the
// operator the same thing they would have had in a terminal — a live log — so
// the launch is not a fire-and-forget that leaves them refreshing the run index
// wondering whether it took.
//
// The plan picker has a free-text path beside it rather than instead of it. The
// picker only knows the conventional locations, and a plan living anywhere else
// is a completely ordinary case; a picker that cannot express it would send the
// operator back to the terminal for exactly the thing this page exists to
// avoid.

import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  errorMessage,
  getResolvedOptions,
  listGroupings,
  listPlans,
  listRuns,
  startGroupJob,
  startResumeJob,
  startRunJob,
} from "../api";
import ExecutionOptionsForm from "../components/launch/ExecutionOptions";
import GroupingPreview from "../components/launch/GroupingPreview";
import JobLog from "../components/launch/JobLog";
import type {
  ExecutionOptions,
  GroupingSummary,
  GroupJobBody,
  JobInfo,
  PlanDoc,
  ResolvedOptions,
  RunInfo,
} from "../types";
import "./Launch.css";

const GRANULARITIES: NonNullable<GroupJobBody["granularity"]>[] = [
  "independent",
  "balanced",
  "monolithic",
];

// How often the three launch-form lists are refetched while the page stays
// open. A grouping finishing, or a job started elsewhere, should become
// visible without a reload — this is what makes that true without a push
// channel for any of the three (F3, F6).
const LAUNCH_REFRESH_MS = 5000;

export function Launch() {
  const { project = "" } = useParams();
  const [plans, setPlans] = useState<PlanDoc[]>([]);
  const [groupings, setGroupings] = useState<GroupingSummary[]>([]);
  const [runs, setRuns] = useState<RunInfo[]>([]);
  const [resolved, setResolved] = useState<ResolvedOptions | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [job, setJob] = useState<JobInfo | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function refresh(): Promise<void> {
      try {
        const [nextPlans, nextGroupings, nextRuns, nextResolved] = await Promise.all([
          listPlans(project),
          listGroupings(project),
          listRuns(project),
          getResolvedOptions(project),
        ]);
        if (cancelled) return;
        setPlans(nextPlans);
        setGroupings(nextGroupings);
        setRuns(nextRuns);
        setResolved(nextResolved);
        // A refetch that succeeds after a prior one failed clears the error —
        // but a *failed* refetch below never touches `plans`/`groupings`/
        // `runs`, so the last known-good data stays on screen instead of the
        // form blanking out from under the operator.
        setLoadError(null);
      } catch (err) {
        if (!cancelled) setLoadError(errorMessage(err));
      }
    }

    void refresh();
    const timer = window.setInterval(() => void refresh(), LAUNCH_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [project]);

  return (
    <div className="app">
      <header className="app__header">
        <div>
          <h1>
            <Link className="app__title-link" to="/">
              Orchestrator Observatory
            </Link>
          </h1>
          <p className="app__run-id">
            <Link to={`/p/${encodeURIComponent(project)}`}>{project}</Link> / launch
          </p>
        </div>
      </header>

      {loadError && <p className="app__error">{loadError}</p>}

      <div className="launch__cards">
        <GroupCard project={project} plans={plans} onLaunched={setJob} />
        <RunCard project={project} groupings={groupings} resolved={resolved} onLaunched={setJob} />
        <ResumeCard project={project} runs={runs} resolved={resolved} onLaunched={setJob} />
      </div>

      {job && (
        <p className="launch__job-link">
          <Link to={`/p/${encodeURIComponent(project)}/jobs/${encodeURIComponent(job.job_id)}`}>
            Open this job's own page
          </Link>
        </p>
      )}
      <RunHeader job={job} resolved={resolved} />
      <JobLog project={project} job={job} />
    </div>
  );
}

/** The one place a launch's outcome becomes UI: the job on success, the
 * backend's own `detail` on failure — including the 409 that refuses a second
 * scheduler over a live run, which is a message worth reading verbatim. */
function useLaunch(onLaunched: (job: JobInfo) => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const launch = async (start: () => Promise<JobInfo>) => {
    setBusy(true);
    setError(null);
    try {
      onLaunched(await start());
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return { busy, error, launch };
}

function GroupCard({
  project,
  plans,
  onLaunched,
}: {
  project: string;
  plans: PlanDoc[];
  onLaunched: (job: JobInfo) => void;
}) {
  const [plan, setPlan] = useState("");
  const [name, setName] = useState("");
  const [dryRun, setDryRun] = useState(false);
  const [granularity, setGranularity] = useState<GroupJobBody["granularity"]>(null);
  const [tokenBudget, setTokenBudget] = useState("");
  // Three-state, matching `auto_resume` on the run/resume forms: unset (null)
  // lets the CLI's own default stand, unticking sends the explicit opt-out.
  const [autoResume, setAutoResume] = useState<boolean | null>(null);
  const { busy, error, launch } = useLaunch(onLaunched);

  return (
    <section className="launch__card" aria-label="Group a plan">
      <h2>Group a plan</h2>
      <label htmlFor="launch-plan-pick">
        Plan document
        <select
          id="launch-plan-pick"
          value={plans.some((p) => p.path === plan) ? plan : ""}
          onChange={(e) => setPlan(e.target.value)}
        >
          <option value="">(choose or type a path)</option>
          {plans.map((doc) => (
            <option key={doc.path} value={doc.path}>
              {doc.title} — {doc.path}
            </option>
          ))}
        </select>
      </label>
      <label htmlFor="launch-plan-path">
        …or a path, relative to the repo
        <input
          id="launch-plan-path"
          type="text"
          value={plan}
          placeholder="docs/plans/my-plan.md"
          onChange={(e) => setPlan(e.target.value)}
        />
      </label>
      <label htmlFor="launch-group-name">
        Grouping name
        <input
          id="launch-group-name"
          type="text"
          value={name}
          placeholder="(the plan's filename stem)"
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label htmlFor="launch-granularity">
        Granularity
        <select
          id="launch-granularity"
          value={granularity ?? ""}
          onChange={(e) =>
            setGranularity((e.target.value || null) as GroupJobBody["granularity"])
          }
        >
          <option value="">(from config)</option>
          {GRANULARITIES.map((g) => (
            <option key={g} value={g}>
              {g}
            </option>
          ))}
        </select>
      </label>
      <label htmlFor="launch-token-budget">
        Token budget
        <input
          id="launch-token-budget"
          type="number"
          min={0}
          value={tokenBudget}
          placeholder="(from config)"
          onChange={(e) => setTokenBudget(e.target.value)}
        />
      </label>
      <label className="launch__check" htmlFor="launch-dry-run">
        <input
          id="launch-dry-run"
          type="checkbox"
          checked={dryRun}
          onChange={(e) => setDryRun(e.target.checked)}
        />
        Dry run (print the groups, write nothing)
      </label>
      <label className="launch__check" htmlFor="launch-group-auto-resume">
        <input
          id="launch-group-auto-resume"
          type="checkbox"
          checked={autoResume !== false}
          onChange={(e) => setAutoResume(e.target.checked ? null : false)}
        />
        Wait out usage limits
      </label>
      {error && <p className="app__error">{error}</p>}
      <button
        type="button"
        disabled={busy || !plan.trim()}
        onClick={() =>
          void launch(() =>
            startGroupJob(project, {
              plan: plan.trim(),
              name: name.trim() || null,
              granularity: granularity || null,
              token_budget: tokenBudget.trim() === "" ? null : Number(tokenBudget),
              dry_run: dryRun,
              auto_resume: autoResume,
            }),
          )
        }
      >
        {busy ? "Starting…" : "Group"}
      </button>
    </section>
  );
}

/** The resolved concurrency and three model ids a run or resume job actually
 * used — the job's own submitted options where set, falling back to the
 * project's resolved defaults for whatever was left unspecified. This is what
 * makes a running job's effective settings legible on the page that launched
 * it, rather than only visible as a raw argv (F14/U18). */
function RunHeader({
  job,
  resolved,
}: {
  job: JobInfo | null;
  resolved: ResolvedOptions | null;
}) {
  if (!job || !resolved || job.kind === "group") return null;
  const submitted = (job.options as { options?: ExecutionOptions } | undefined)?.options ?? {};
  const concurrency = submitted.concurrency ?? resolved.concurrency;
  const modelWorker = submitted.model_worker ?? resolved.model_worker;
  const modelBase = submitted.model_base ?? resolved.model_base;
  const modelSpeccer = submitted.model_speccer ?? resolved.model_speccer;
  return (
    <p className="launch__run-header">
      Resolved for this run: concurrency {concurrency} · worker model {modelWorker} · base model{" "}
      {modelBase} · speccer model {modelSpeccer}
    </p>
  );
}

function RunCard({
  project,
  groupings,
  resolved,
  onLaunched,
}: {
  project: string;
  groupings: GroupingSummary[];
  resolved: ResolvedOptions | null;
  onLaunched: (job: JobInfo) => void;
}) {
  const [grouping, setGrouping] = useState("");
  const [runId, setRunId] = useState("");
  const [options, setOptions] = useState<ExecutionOptions>({});
  const { busy, error, launch } = useLaunch(onLaunched);

  return (
    <section className="launch__card" aria-label="Start a run">
      <h2>Start a run</h2>
      <label htmlFor="launch-grouping">
        Grouping
        <select
          id="launch-grouping"
          value={grouping}
          onChange={(e) => setGrouping(e.target.value)}
        >
          {/* Empty is meaningful, not a placeholder: the CLI auto-selects when
              exactly one grouping exists and reports the ambiguity otherwise. */}
          <option value="">(auto-select if only one)</option>
          {groupings.map((entry) => (
            <option key={entry.name} value={entry.name}>
              {entry.name} — {entry.group_count} group(s)
            </option>
          ))}
        </select>
      </label>
      <GroupingPreview project={project} name={grouping} />
      <label htmlFor="launch-run-id">
        Run id
        <input
          id="launch-run-id"
          type="text"
          value={runId}
          placeholder="(r<timestamp>)"
          onChange={(e) => setRunId(e.target.value)}
        />
      </label>
      <ExecutionOptionsForm
        idPrefix="run"
        value={options}
        onChange={setOptions}
        disabled={busy}
        resolved={resolved}
      />
      {error && <p className="app__error">{error}</p>}
      <button
        type="button"
        disabled={busy}
        onClick={() =>
          void launch(() =>
            startRunJob(project, {
              grouping: grouping || null,
              run_id: runId.trim() || null,
              options,
            }),
          )
        }
      >
        {busy ? "Starting…" : "Start run"}
      </button>
    </section>
  );
}

function ResumeCard({
  project,
  runs,
  resolved,
  onLaunched,
}: {
  project: string;
  runs: RunInfo[];
  resolved: ResolvedOptions | null;
  onLaunched: (job: JobInfo) => void;
}) {
  const [runId, setRunId] = useState("");
  const [options, setOptions] = useState<ExecutionOptions>({});
  const { busy, error, launch } = useLaunch(onLaunched);

  return (
    <section className="launch__card" aria-label="Resume a run">
      <h2>Resume a run</h2>
      <label htmlFor="launch-resume-run">
        Run
        <select id="launch-resume-run" value={runId} onChange={(e) => setRunId(e.target.value)}>
          <option value="">(choose a run)</option>
          {runs.map((run) => (
            <option key={run.run_id} value={run.run_id}>
              {run.run_id}
            </option>
          ))}
        </select>
      </label>
      <p className="launch__hint">
        The run keeps the escalation tier and auto-resume setting it was started with. Anything
        set here overrides them — which is the whole reason these controls are on the form
        rather than left to a remembered flag.
      </p>
      <ExecutionOptionsForm
        idPrefix="resume"
        value={options}
        onChange={setOptions}
        disabled={busy}
        resolved={resolved}
      />
      {error && <p className="app__error">{error}</p>}
      <button
        type="button"
        disabled={busy || !runId}
        onClick={() => void launch(() => startResumeJob(project, { run_id: runId, options }))}
      >
        {busy ? "Starting…" : "Resume"}
      </button>
    </section>
  );
}

export default Launch;
