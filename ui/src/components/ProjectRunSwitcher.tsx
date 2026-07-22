// Project → run selection, all client-side state: changing either select swaps
// the active run without a page reload. Backend access goes through `api.ts`
// (never raw fetch), and a registry entry whose repo is unusable is shown
// disabled with its `error` text rather than dropped.

import { useEffect, useState } from "react";

import { errorMessage, listProjects, listRuns } from "../api";
import type { Project, RunInfo } from "../types";

interface ProjectRunSwitcherProps {
  project: string | null;
  runId: string | null;
  onProjectChange: (project: string | null) => void;
  onRunChange: (runId: string | null) => void;
}

function ProjectRunSwitcher({
  project,
  runId,
  onProjectChange,
  onRunChange,
}: ProjectRunSwitcherProps) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [runs, setRuns] = useState<RunInfo[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listProjects()
      .then((next) => {
        if (cancelled) return;
        setProjects(next);
        setError(null);
      })
      .catch((err) => {
        if (!cancelled) setError(errorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    setRuns([]);
    if (!project) return;
    let cancelled = false;
    listRuns(project)
      .then((next) => {
        if (cancelled) return;
        setRuns(next);
        setError(null);
        // The backend lists newest first; auto-select it so picking a project
        // immediately shows a run instead of an empty board.
        onRunChange(next.length > 0 ? next[0].run_id : null);
      })
      .catch((err) => {
        if (!cancelled) setError(errorMessage(err));
      });
    return () => {
      cancelled = true;
    };
    // Re-fetch only on project switch; onRunChange is App's stable setter.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project]);

  return (
    <div className="switcher">
      <label className="switcher__field">
        <span className="switcher__label">Project</span>
        <select
          value={project ?? ""}
          onChange={(event) => onProjectChange(event.target.value || null)}
        >
          <option value="">— select —</option>
          {projects.map((entry) => (
            <option key={entry.name} value={entry.name} disabled={Boolean(entry.error)}>
              {entry.name}
              {entry.error ? ` — ${entry.error}` : ""}
            </option>
          ))}
        </select>
      </label>
      <label className="switcher__field">
        <span className="switcher__label">Run</span>
        <select
          value={runId ?? ""}
          disabled={!project}
          onChange={(event) => onRunChange(event.target.value || null)}
        >
          <option value="">— select —</option>
          {runs.map((run) => (
            <option key={run.run_id} value={run.run_id}>
              {run.run_id}
            </option>
          ))}
        </select>
      </label>
      {error && <span className="switcher__error">{error}</span>}
      {!error && projects.length === 0 && (
        <span className="switcher__hint">no projects registered</span>
      )}
    </div>
  );
}

export default ProjectRunSwitcher;
