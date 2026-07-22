// The one file that composes the Observatory: the switcher picks the active
// (project, run), `useRunStream` keeps that run's snapshot live, and the four
// slice components each receive the active selection. This file is owned by the
// spa-shell group alone — slice groups fill in their own components, never this.

import { useState } from "react";

import EscalationPanel from "./components/EscalationPanel";
import EventLog from "./components/EventLog";
import GroupBoard from "./components/GroupBoard";
import GroupDrillIn from "./components/GroupDrillIn";
import ProjectRunSwitcher from "./components/ProjectRunSwitcher";
import { useRunStream } from "./useRunStream";

function App() {
  const [project, setProject] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const { snapshot, revision, error, loading } = useRunStream(project, runId);

  return (
    <div className="app">
      <header className="app__header">
        <div>
          <h1>Orchestrator Observatory</h1>
          {project && runId && (
            <p className="app__run-id">
              {project} / {runId}
            </p>
          )}
        </div>
        <ProjectRunSwitcher
          project={project}
          runId={runId}
          onProjectChange={(next) => {
            setProject(next);
            setRunId(null);
          }}
          onRunChange={setRunId}
        />
      </header>

      {error && <p className="app__error">{error}</p>}

      {project && runId ? (
        <>
          <GroupBoard
            project={project}
            runId={runId}
            snapshot={snapshot}
            revision={revision}
            loading={loading}
          />
          <EventLog project={project} runId={runId} />
          <EscalationPanel project={project} runId={runId} revision={revision} />
          <GroupDrillIn project={project} runId={runId} snapshot={snapshot} revision={revision} />
        </>
      ) : (
        <p className="app__empty">Select a project and run to observe.</p>
      )}
    </div>
  );
}

export default App;
