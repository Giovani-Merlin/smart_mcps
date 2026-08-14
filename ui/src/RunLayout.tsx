// The shell around a run: header, switcher, tab strip, paths drawer, outlet.
//
// One `useRunStream` for the whole run lives here and reaches the tabs through
// the outlet context, so switching tabs does not re-open the stream and two
// tabs can never disagree about what the snapshot says.
//
// The tabs are `NavLink`s, not buttons. That is the point of the router: a tab
// is a URL, so it can be linked, bookmarked, opened in a new window with a
// middle click, and refreshed. The session viewer is deliberately absent from
// the strip — it is route-addressable (`…/session/:groupId/:sessionId`) but it
// is not a tab, because it is a thing you open, not a place you are.
//
// Write surfaces. This used to say "no launching, resuming or aborting a run
// from the UI" — that non-goal has been deliberately reversed. Launching lives
// at `/p/:project/launch`, and the Resume link in this header points at it with
// the run pre-chosen. What reversed it: `--intensity` turned out to be
// droppable on a terminal `resume`, silently reverting a run to block-forever
// HITL, and a form that shows the tier as a field is a *safer* surface than a
// flag someone has to remember. Aborting a run is still absent, and editing
// plans or config still is too.
//
// This layout also carries the usage-limit banner, because "paused" is a
// property of the whole run rather than of any one tab: a limited run stops
// moving on every tab at once, and without the banner that is indistinguishable
// from wedged.

import { NavLink, Outlet, useNavigate, useOutletContext, useParams } from "react-router-dom";

import PathsDrawer from "./components/PathsDrawer";
import ProjectRunSwitcher from "./components/ProjectRunSwitcher";
import UsageLimitBanner from "./components/UsageLimitBanner";
import { useRunStream } from "./useRunStream";
import type { RunSnapshot } from "./types";

/** What every tab under a run gets, through `useOutletContext`. */
export interface RunContext {
  project: string;
  runId: string;
  snapshot: RunSnapshot | null;
  revision: number;
  loading: boolean;
}

export function useRunContext(): RunContext {
  return useOutletContext<RunContext>();
}

/** Terminology is fixed by the plan: run → group → generation → session → round. */
export const RUN_TABS = [
  { path: "board", label: "Board" },
  { path: "history", label: "History" },
  { path: "grouping", label: "Grouping" },
  { path: "escalations", label: "Escalations" },
  { path: "log", label: "Log" },
  { path: "cost", label: "Cost" },
] as const;

export function RunLayout() {
  const { project = "", runId = "" } = useParams();
  const navigate = useNavigate();
  const { snapshot, revision, error, loading } = useRunStream(project, runId);
  const hasInterrupted = Boolean(
    snapshot?.groups.some((group) => group.state === "interrupted"),
  );

  return (
    <div className="app">
      <header className="app__header">
        <div>
          <h1>
            <NavLink className="app__title-link" to="/">
              Orchestrator Observatory
            </NavLink>
          </h1>
          <p className="app__run-id">
            {project} / {runId}
          </p>
        </div>
        {/* Offered only when there is something to resume. A Resume button on a
          * run that is running invites the 409 the backend exists to refuse. */}
        {hasInterrupted && (
          <NavLink className="launch__entry" to={`/p/${encodeURIComponent(project)}/launch`}>
            Resume this run
          </NavLink>
        )}
        <ProjectRunSwitcher
          project={project}
          runId={runId}
          // Switching project lands on that project's run index rather than
          // guessing a run — guessing is the auto-jump-to-newest the index
          // exists to fix.
          onProjectChange={(next) => navigate(next ? `/p/${encodeURIComponent(next)}` : "/")}
          onRunChange={(next) =>
            navigate(
              next
                ? `/p/${encodeURIComponent(project)}/r/${encodeURIComponent(next)}`
                : `/p/${encodeURIComponent(project)}`,
            )
          }
        />
      </header>

      <UsageLimitBanner usageLimit={snapshot?.usage_limit} />

      {error && <p className="app__error">{error}</p>}

      <nav className="app__tabs" aria-label="Run views">
        {RUN_TABS.map((tab) => (
          <NavLink
            key={tab.path}
            to={tab.path}
            className={({ isActive }) => `app__tab${isActive ? " app__tab--current" : ""}`}
          >
            {tab.label}
          </NavLink>
        ))}
      </nav>

      <PathsDrawer project={project} runId={runId} />

      <Outlet context={{ project, runId, snapshot, revision, loading } satisfies RunContext} />
    </div>
  );
}

export default RunLayout;
