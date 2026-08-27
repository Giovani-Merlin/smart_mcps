// `/p/:project/jobs/:id` — one job's log, addressable and reloadable.
//
// Reusing `JobLog` rather than re-implementing streaming here: it already
// opens `/events/job` on mount, so a reload of this page resumes streaming a
// still-running job exactly the way the launch page's live view does. This
// route's own job is just resolving the `:id` into the `JobInfo` `JobLog`
// needs, and refetching it — a job started elsewhere, or an id that has never
// existed, has to be distinguished from a page that is merely still loading.

import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, errorMessage, getJob } from "../api";
import JobLog from "../components/launch/JobLog";
import type { JobInfo } from "../types";
import "./Launch.css";

// How often the job record itself (not just its log) is refetched, so kind,
// pid and started_at stay current for a job discovered by navigating here
// directly rather than just-launched from the form.
const JOB_REFRESH_MS = 3000;

export function JobDetail() {
  const { project = "", id = "" } = useParams();
  const [job, setJob] = useState<JobInfo | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setJob(null);
    setNotFound(false);
    setError(null);

    async function refresh(): Promise<void> {
      try {
        const next = await getJob(project, id);
        if (cancelled) return;
        setJob(next);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          setNotFound(true);
          window.clearInterval(timer);
        } else {
          setError(errorMessage(err));
        }
      }
    }

    void refresh();
    const timer = window.setInterval(() => void refresh(), JOB_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [project, id]);

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
            <Link to={`/p/${encodeURIComponent(project)}`}>{project}</Link> /{" "}
            <Link to={`/p/${encodeURIComponent(project)}/jobs`}>jobs</Link> / {id}
          </p>
        </div>
      </header>

      {notFound && <p className="app__empty">No job {id} was found for this project.</p>}
      {error && <p className="app__error">{error}</p>}
      {!notFound && !job && !error && <p className="app__empty">Loading job…</p>}
      {job && <JobLog project={project} job={job} />}
    </div>
  );
}

export default JobDetail;
