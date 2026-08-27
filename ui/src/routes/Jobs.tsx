// `/p/:project/jobs` — every job launched from this project, addressable.
//
// A launched job used to live only in `Launch`'s component state: a refresh,
// a navigation away, or a second tab lost sight of it entirely. This route is
// backed by the same `GET /api/projects/{p}/jobs` the launch page already
// used to check for a live run, listed here on its own so a job never
// disappears just because the tab that started it closed.

import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { errorMessage, listJobs } from "../api";
import type { JobInfo } from "../types";
import "./Launch.css";

function formatStarted(iso: string | null | undefined): string {
  if (!iso) return "";
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString();
}

export function Jobs() {
  const { project = "" } = useParams();
  const [jobs, setJobs] = useState<JobInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setJobs(null);
    setError(null);
    void (async () => {
      try {
        const next = await listJobs(project);
        if (!cancelled) setJobs(next);
      } catch (err) {
        if (!cancelled) setError(errorMessage(err));
      }
    })();
    return () => {
      cancelled = true;
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
            <Link to={`/p/${encodeURIComponent(project)}`}>{project}</Link> / jobs
          </p>
        </div>
        <Link className="launch__entry" to={`/p/${encodeURIComponent(project)}/launch`}>
          New run
        </Link>
      </header>

      <h2>Jobs</h2>
      {error && <p className="app__error">{error}</p>}
      {jobs === null && !error && <p className="app__empty">Loading jobs…</p>}
      {jobs !== null && jobs.length === 0 && (
        <p className="app__empty">Nothing has been launched from this project yet.</p>
      )}
      {jobs !== null && jobs.length > 0 && (
        <ul className="run-index__list">
          {jobs.map((job) => (
            <li key={job.job_id} className="run-index__row">
              <Link
                className="run-index__link"
                to={`/p/${encodeURIComponent(project)}/jobs/${encodeURIComponent(job.job_id)}`}
              >
                {job.kind} · {job.job_id}
              </Link>
              <span className="run-index__meta">{job.running ? "running" : "exited"}</span>
              {job.started_at && (
                <span className="run-index__meta">started {formatStarted(job.started_at)}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default Jobs;
