// The route table. Path segments identify objects; query params identify view
// state — that split is the whole information architecture:
//
//   /                                          project picker
//   /p/:project                                run index (no auto-jump)
//   /p/:project/launch                         group / run / resume
//   /p/:project/jobs                           every launched job, addressable
//   /p/:project/jobs/:id                       one job's log, reloadable
//   /p/:project/r/:runId/board                 Board
//   /p/:project/r/:runId/history               attempt grid
//   /p/:project/r/:runId/grouping?stage=&edge= how this plan became groups
//   /p/:project/r/:runId/escalations           the one write surface
//   /p/:project/r/:runId/log                   run.log, full height
//   /p/:project/r/:runId/cost                  estimate vs actual
//   /p/:project/r/:runId/session/:groupId[/:sessionId]   session viewer
//
// Exported as a plain `RouteObject[]` rather than a built router so tests can
// mount the same table under `createMemoryRouter`: a route test that describes
// its own routes proves nothing about the app.
//
// Deep links depend on the backend's SPA catch-all, which is landed
// (`observatory/app.py:_mount_spa`) — anything not under a route prefix the
// server owns falls through to `index.html`. That is why this is
// `createBrowserRouter` and not `HashRouter`.

import { Navigate, useNavigate, useParams } from "react-router-dom";
import type { RouteObject } from "react-router-dom";

import AttemptGrid from "./components/AttemptGrid";
import CostPanel from "./components/CostPanel";
import EscalationPanel from "./components/EscalationPanel";
import EventLog from "./components/EventLog";
import GroupBoard from "./components/GroupBoard";
import GroupDrillIn from "./components/GroupDrillIn";
import GroupingTab from "./components/grouping/GroupingTab";
import JobDetail from "./routes/JobDetail";
import Jobs from "./routes/Jobs";
import Launch from "./routes/Launch";
import ProjectIndex from "./routes/ProjectIndex";
import ProjectPicker from "./routes/ProjectPicker";
import RunLayout, { useRunContext } from "./RunLayout";
import { useQueryParams } from "./useQueryParams";

// ------------------------------------------------------------------- tabs

function BoardTab() {
  const { project, runId, snapshot, revision, loading } = useRunContext();
  const [params] = useQueryParams();
  return (
    <>
      <GroupBoard
        project={project}
        runId={runId}
        snapshot={snapshot}
        revision={revision}
        loading={loading}
      />
      {/* `?group=` selects a card's drill-in without leaving the board. The
        * full session viewer has its own route. */}
      <GroupDrillIn
        project={project}
        runId={runId}
        snapshot={snapshot}
        revision={revision}
        selectedGroupId={params.get("group")}
        selectedSessionId={params.get("session")}
      />
    </>
  );
}

function HistoryTab() {
  const { project, runId, snapshot, revision } = useRunContext();
  const navigate = useNavigateToSession();
  return (
    <AttemptGrid
      project={project}
      runId={runId}
      snapshot={snapshot}
      revision={revision}
      onOpenSession={navigate}
    />
  );
}

function GroupingRoute() {
  const { project, runId } = useRunContext();
  const [params, setParams] = useQueryParams();
  return (
    <GroupingTab project={project} runId={runId} params={params} onParamsChange={setParams} />
  );
}

function EscalationsTab() {
  const { project, runId, revision } = useRunContext();
  return <EscalationPanel project={project} runId={runId} revision={revision} />;
}

// The log gets the whole route rather than a pane stacked under the board —
// that is what "promoted to full height" means, and it is a CSS modifier on
// the wrapper, not a change to `EventLog`.
function LogTab() {
  const { project, runId } = useRunContext();
  return (
    <div className="app__tab-pane--log">
      <EventLog project={project} runId={runId} />
    </div>
  );
}

function CostTab() {
  const { project, runId, snapshot } = useRunContext();
  return <CostPanel project={project} runId={runId} snapshot={snapshot} />;
}

/**
 * The session viewer: route-addressable, deliberately not a tab.
 *
 * `GroupDrillIn` already takes a seeded selection, so the path segments feed
 * straight into the props it has — no component changed to be reachable by URL.
 */
function SessionRoute() {
  const { project, runId, snapshot, revision } = useRunContext();
  const { groupId = null, sessionId = null } = useParams();
  return (
    <GroupDrillIn
      project={project}
      runId={runId}
      snapshot={snapshot}
      revision={revision}
      selectedGroupId={groupId}
      selectedSessionId={sessionId}
    />
  );
}

/** Opens the session viewer's route — what the attempt grid's cells call. */
function useNavigateToSession(): (groupId: string, sessionId: string) => void {
  const { project, runId } = useRunContext();
  const navigate = useNavigate();
  return (groupId, sessionId) =>
    navigate(
      `/p/${encodeURIComponent(project)}/r/${encodeURIComponent(runId)}` +
        `/session/${encodeURIComponent(groupId)}/${encodeURIComponent(sessionId)}`,
    );
}

// ----------------------------------------------------------------- table

export const routes: RouteObject[] = [
  { path: "/", element: <ProjectPicker /> },
  { path: "/p", element: <Navigate to="/" replace /> },
  { path: "/p/:project", element: <ProjectIndex /> },
  { path: "/p/:project/launch", element: <Launch /> },
  { path: "/p/:project/jobs", element: <Jobs /> },
  { path: "/p/:project/jobs/:id", element: <JobDetail /> },
  {
    path: "/p/:project/r/:runId",
    element: <RunLayout />,
    children: [
      { index: true, element: <Navigate to="board" replace /> },
      { path: "board", element: <BoardTab /> },
      { path: "history", element: <HistoryTab /> },
      { path: "grouping", element: <GroupingRoute /> },
      { path: "escalations", element: <EscalationsTab /> },
      { path: "log", element: <LogTab /> },
      { path: "cost", element: <CostTab /> },
      { path: "session/:groupId", element: <SessionRoute /> },
      { path: "session/:groupId/:sessionId", element: <SessionRoute /> },
    ],
  },
  // An unknown path is a mistyped or stale link, not an error state worth a
  // page: send it to the picker, which is where it can be corrected.
  { path: "*", element: <Navigate to="/" replace /> },
];

export default routes;
