// The landing route: pick a project, land on its run index.
//
// `ProjectRunSwitcher` is reused unchanged. Its run half auto-selects the
// newest run when none is selected, which is exactly the behaviour the run
// index exists to escape — so here it is mounted with no project, which means
// the run fetch never fires and nothing is auto-selected. Choosing a project
// navigates to `/p/:project`, where the switcher is not mounted at all.

import { useNavigate } from "react-router-dom";

import ProjectRunSwitcher from "../components/ProjectRunSwitcher";

export function ProjectPicker() {
  const navigate = useNavigate();

  return (
    <div className="app">
      <header className="app__header">
        <div>
          <h1>Orchestrator Observatory</h1>
        </div>
        <ProjectRunSwitcher
          project={null}
          runId={null}
          onProjectChange={(next) => {
            if (next) navigate(`/p/${encodeURIComponent(next)}`);
          }}
          onRunChange={() => {
            /* No project is selected, so there are no runs to change to. */
          }}
        />
      </header>
      <p className="app__empty">Select a project to see its runs.</p>
    </div>
  );
}

export default ProjectPicker;
