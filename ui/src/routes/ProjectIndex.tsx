// `/p/:project` — every run this project has, listed.
//
// This route exists to fix a specific dead end. Selecting a project used to
// auto-jump to whichever run was newest, which meant there was no page that
// showed the others: an older run was reachable only by knowing it existed and
// picking it out of a select. Worse, the auto-jump also fired on a deep link,
// so a URL shared with a colleague could land them on a different run than the
// one being discussed.
//
// So: no redirect, no auto-selection. A list of links, newest first, exactly as
// the backend orders them. Choosing one is a navigation the operator made.

import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { errorMessage, listRuns } from "../api";
import type { RunInfo } from "../types";
import "./Launch.css";

function formatUpdated(iso: string | null | undefined): string {
  if (!iso) return "";
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString();
}

export function ProjectIndex() {
  const { project = "" } = useParams();
  const [runs, setRuns] = useState<RunInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRuns(null);
    setError(null);
    void (async () => {
      try {
        const next = await listRuns(project);
        if (!cancelled) setRuns(next);
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
          <p className="app__run-id">{project}</p>
        </div>
        {/* The entry point into the launch surface. It sits here rather than on
          * a run, because grouping and starting are things you do *before* a
          * run exists — there is no run page to hang them off. */}
        <Link className="launch__entry" to={`/p/${encodeURIComponent(project)}/launch`}>
          New run
        </Link>
      </header>

      <h2>Runs</h2>
      {error && <p className="app__error">{error}</p>}
      {runs === null && !error && <p className="app__empty">Loading runs…</p>}
      {runs !== null && runs.length === 0 && (
        <p className="app__empty">This project has no runs yet.</p>
      )}
      {runs !== null && runs.length > 0 && (
        <>
          <ul className="run-index__list">
            {runs.map((run) => (
              <li key={run.run_id} className="run-index__row">
                <Link
                  className="run-index__link"
                  to={`/p/${encodeURIComponent(project)}/r/${encodeURIComponent(run.run_id)}/board`}
                >
                  {run.run_id}
                </Link>
                {run.updated_at && (
                  <span className="run-index__meta">updated {formatUpdated(run.updated_at)}</span>
                )}
              </li>
            ))}
          </ul>
          <p className="run-index__note">Newest first, as the backend lists them.</p>
        </>
      )}
    </div>
  );
}

export default ProjectIndex;
