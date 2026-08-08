// The one file that composes the Observatory: the switcher picks the active
// (project, run), `useRunStream` keeps that run's snapshot live, and the slice
// components each receive the active selection.
//
// The tab strip below is deliberately the smallest thing that works. The agreed
// shell is a `react-router-dom` v6 tab shell owned by the shell group — Board,
// History, Grouping, Escalations, Log — and building a second router here would
// be the parallel copy that has to be unpicked later. So the active tab lives in
// `?tab=`, read through `useQueryParams`, which has `useSearchParams`' shape: the
// shell replaces this strip with real routes and every child keeps compiling.

import EscalationPanel from "./components/EscalationPanel";
import EventLog from "./components/EventLog";
import GroupBoard from "./components/GroupBoard";
import GroupDrillIn from "./components/GroupDrillIn";
import ProjectRunSwitcher from "./components/ProjectRunSwitcher";
import GroupingTab from "./components/grouping/GroupingTab";
import { useQueryParams } from "./useQueryParams";
import { useRunStream } from "./useRunStream";

const TABS = ["board", "grouping"] as const;
type Tab = (typeof TABS)[number];

function App() {
  const [params, setParams] = useQueryParams();
  const project = params.get("project");
  const runId = params.get("run");
  const tab: Tab = TABS.includes(params.get("tab") as Tab) ? (params.get("tab") as Tab) : "board";
  const { snapshot, revision, error, loading } = useRunStream(project, runId);

  function setParam(key: string, value: string | null): void {
    const next = new URLSearchParams(params);
    if (value === null) next.delete(key);
    else next.set(key, value);
    setParams(next);
  }

  function selectProject(next: string | null): void {
    const params = new URLSearchParams();
    if (next) params.set("project", next);
    setParams(params);
  }

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
          onProjectChange={selectProject}
          onRunChange={(next) => setParam("run", next)}
        />
      </header>

      {error && <p className="app__error">{error}</p>}

      {project && runId ? (
        <>
          <nav className="app__tabs">
            {TABS.map((candidate) => (
              <button
                key={candidate}
                type="button"
                className={`app__tab${candidate === tab ? " app__tab--current" : ""}`}
                onClick={() => setParam("tab", candidate)}
                aria-current={candidate === tab}
              >
                {candidate}
              </button>
            ))}
          </nav>

          {tab === "grouping" ? (
            <GroupingTab
              project={project}
              runId={runId}
              params={params}
              onParamsChange={setParams}
            />
          ) : (
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
              <GroupDrillIn
                project={project}
                runId={runId}
                snapshot={snapshot}
                revision={revision}
              />
            </>
          )}
        </>
      ) : (
        <p className="app__empty">Select a project and run to observe.</p>
      )}
    </div>
  );
}

export default App;
