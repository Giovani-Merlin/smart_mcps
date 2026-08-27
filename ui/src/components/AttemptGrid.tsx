// The History tab: every attempt a group ever made, as a grid.
//
// Rows are groups, columns are generations, cells are coloured and clickable —
// the Airflow Grid shape. LangSmith's implicit nested retry rows were the
// rejected alternative: they hide a retry inside a row you have to expand, and
// the whole complaint this tab answers is that retries were invisible.
//
// The data comes from `manifest.json` by way of the snapshot's append-only
// `sessions` list, which is why a group whose `state.json` says "generation 2,
// completed" still shows both of its attempts here. `buildAttemptGrid` in
// `attempts.ts` does all of the deriving; this file only renders it, and it
// takes every colour from `status.ts` — there is deliberately no status→colour
// logic in this component.

import { useEffect, useMemo, useState } from "react";

import { errorMessage, getRunPaths, listEscalations } from "../api";
import { buildAttemptGrid, sessionBaseName, sessionGeneration } from "../attempts";
import type { AttemptCell, AttemptNote } from "../attempts";
import { ATTENTION_COLOUR } from "../status";
import type { EscalationRequest, RunSnapshot, SnapshotSession } from "../types";
import PathChip from "./PathChip";
import "./AttemptGrid.css";

export interface AttemptGridProps {
  project: string;
  runId: string;
  snapshot: RunSnapshot | null;
  /** Advances on every run-directory change; escalations refresh off it. */
  revision: number;
  /** Opens the route-addressable session viewer on the Board tab. */
  onOpenSession?: (groupId: string, sessionId: string) => void;
  /** Injected by tests so "no activity for 23m" is a fixed string. */
  nowMs?: number;
}

/** Notes that are about attention rather than history get amber. Everything
 * else is neutral: a note is a fact on a cell, not a second status. */
const ATTENTION_NOTES = new Set(["escalation_blocked", "stalled"]);

function noteColour(note: AttemptNote): string | undefined {
  return ATTENTION_NOTES.has(note.kind) ? ATTENTION_COLOUR : undefined;
}

function NoteChip({ note }: { note: AttemptNote }) {
  return (
    <li
      className={`attempt-note attempt-note--${note.kind}`}
      style={{ ["--note-colour" as string]: noteColour(note) }}
      title={note.detail || note.text}
    >
      <span className="attempt-note__text">{note.text}</span>
      {note.copyable && (
        // Copyable text, never a button. Launching, resuming and aborting runs
        // from the UI is an explicit non-goal; showing the command the operator
        // would type is documentation, and a button would be run control.
        <code className="attempt-note__command" data-testid="resume-command">
          {note.copyable}
        </code>
      )}
    </li>
  );
}

function Cell({
  cell,
  selected,
  onSelect,
}: {
  cell: AttemptCell;
  selected: boolean;
  onSelect: () => void;
}) {
  const stall = cell.notes.find((n) => n.kind === "stalled");
  const blocked = cell.notes.find((n) => n.kind === "escalation_blocked");
  const sessions = cell.sessions.length;

  return (
    <button
      type="button"
      className={`attempt-cell${selected ? " attempt-cell--selected" : ""}${
        cell.status.dashed ? " attempt-cell--dashed" : ""
      }`}
      // The colour arrives from `status.ts` already chosen; this component
      // never maps a state to one.
      style={{ ["--cell-colour" as string]: cell.status.colour }}
      aria-pressed={selected}
      aria-label={`${cell.groupId} generation ${cell.generation}, ${cell.status.label}, ${sessions} ${
        sessions === 1 ? "session" : "sessions"
      }`}
      title={`generation ${cell.generation} — ${cell.status.label}`}
      onClick={onSelect}
    >
      <span className="attempt-cell__glyph" aria-hidden="true">
        {cell.status.glyph}
      </span>
      <span className="attempt-cell__sessions">{sessions}</span>
      {blocked && (
        // Solid amber, and orthogonal to the state: a group can be blocked in
        // any of the busy states, so this is an overlay and not the colour.
        <span className="attempt-cell__blocked" title={blocked.detail || blocked.text}>
          !
        </span>
      )}
      {stall && (
        // An inference, marked as one: a `?`, the elapsed fact as its label,
        // and no state colour anywhere near it.
        <span className="attempt-cell__stalled" title={`${stall.text} — ${stall.detail ?? ""}`}>
          ?
        </span>
      )}
    </button>
  );
}

function SessionRow({
  session,
  onOpen,
}: {
  session: SnapshotSession;
  onOpen?: () => void;
}) {
  // The name's trailing `-g<N>` reads as a group reference, not a generation
  // (plan U35/F17) — pulled out into its own badge, and only rendered when the
  // name actually carries one, so a base session gets no label at all rather
  // than a fabricated `gen 0`.
  const generation = sessionGeneration(session.name);
  return (
    <li className="attempt-session">
      <button
        type="button"
        className="attempt-session__open"
        onClick={onOpen}
        disabled={!onOpen}
        title={session.transcript_path ?? "no transcript recorded"}
      >
        <span className={`attempt-session__role attempt-session__role--${session.role}`}>
          {session.role}
        </span>
        {generation !== null && (
          <span className="attempt-session__gen">gen {generation}</span>
        )}
        <span className="attempt-session__name">{sessionBaseName(session.name)}</span>
      </button>
      {session.retirement_reason && (
        <span className="attempt-session__retired">retired: {session.retirement_reason}</span>
      )}
      <span className="attempt-session__rounds">
        {session.rounds_completed} {session.rounds_completed === 1 ? "round" : "rounds"}
      </span>
    </li>
  );
}

function AttemptGrid({
  project,
  runId,
  snapshot,
  revision,
  onOpenSession,
  nowMs,
}: AttemptGridProps) {
  const [escalations, setEscalations] = useState<EscalationRequest[]>([]);
  const [manifestPath, setManifestPath] = useState<string | null>(null);
  const [pathError, setPathError] = useState<string | null>(null);
  const [selected, setSelected] = useState<{ groupId: string; generation: number } | null>(null);

  useEffect(() => {
    setSelected(null);
  }, [project, runId]);

  // Escalation-blocked is orthogonal to state, so it cannot be read off the
  // snapshot — it needs the pending list.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const next = await listEscalations(project, runId);
        if (!cancelled) setEscalations(next);
      } catch {
        // A missing escalations directory is the normal state of most runs;
        // the grid renders without the blocked chips rather than erroring.
        if (!cancelled) setEscalations([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [project, runId, revision]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const view = await getRunPaths(project, runId);
        if (cancelled) return;
        setManifestPath(view.entries.find((entry) => entry.key === "manifest")?.path ?? null);
        setPathError(null);
      } catch (err) {
        if (cancelled) return;
        setManifestPath(null);
        setPathError(errorMessage(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [project, runId]);

  const grid = useMemo(
    () => (snapshot ? buildAttemptGrid(snapshot, { escalations, nowMs }) : null),
    [snapshot, escalations, nowMs],
  );

  const selectedCell: AttemptCell | null = useMemo(() => {
    if (!grid || !selected) return null;
    const row = grid.rows.find((r) => r.group.group_id === selected.groupId);
    return row?.cells.find((c) => c?.generation === selected.generation) ?? null;
  }, [grid, selected]);

  return (
    <section className="attempt-grid" aria-label="Attempt history">
      <div className="attempt-grid__header">
        <h2>Attempt history</h2>
        {/* Exactly one chip per file-backed panel, pointing at the file this
            panel reads: the manifest is the ground truth for what attempts
            existed, and the operator's next move is to go open it. */}
        <PathChip label="manifest" path={manifestPath ?? "manifest.json (path unavailable)"} />
      </div>
      <p className="attempt-grid__note">
        Rows are groups, columns are generations. Every attempt comes from{" "}
        <code>manifest.json</code>, whose session list is append-only.{" "}
        <code>state.json</code> is single-valued and describes only the current
        generation, so it is never used to count attempts.
        {pathError && <span className="attempt-grid__path-error"> ({pathError})</span>}
      </p>

      {!grid ? (
        <p className="attempt-grid__empty">Waiting for the run snapshot…</p>
      ) : grid.rows.length === 0 ? (
        <p className="attempt-grid__empty">This run has no groups.</p>
      ) : (
        <>
          <table className="attempt-grid__table">
            <thead>
              <tr>
                <th scope="col">group</th>
                {grid.generations.map((generation) => (
                  <th key={generation} scope="col">
                    gen {generation}
                  </th>
                ))}
                <th scope="col">attempts</th>
              </tr>
            </thead>
            <tbody>
              {grid.rows.map((row) => (
                <tr key={row.group.group_id} data-testid={`attempt-row-${row.group.group_id}`}>
                  <th scope="row" title={row.group.summary || undefined}>
                    <span className="attempt-grid__group-id">{row.group.group_id}</span>
                    <span className="attempt-grid__group-name">{row.group.name}</span>
                  </th>
                  {row.cells.map((cell, index) => (
                    <td key={grid.generations[index]}>
                      {cell ? (
                        <Cell
                          cell={cell}
                          selected={
                            selected?.groupId === cell.groupId &&
                            selected.generation === cell.generation
                          }
                          onSelect={() =>
                            setSelected((current) =>
                              current?.groupId === cell.groupId &&
                              current.generation === cell.generation
                                ? null
                                : { groupId: cell.groupId, generation: cell.generation },
                            )
                          }
                        />
                      ) : (
                        <span className="attempt-grid__absent" aria-label="no attempt">
                          ·
                        </span>
                      )}
                    </td>
                  ))}
                  <td className="attempt-grid__count">
                    {row.group.sessions.length}{" "}
                    {row.group.sessions.length === 1 ? "session" : "sessions"}
                    {row.retiredSessionCount > 0 && (
                      <span className="attempt-grid__retired-count">
                        {row.retiredSessionCount} retired
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {selectedCell && (
            <div className="attempt-grid__detail" aria-label="Selected attempt">
              <h3>
                {selectedCell.groupId} — generation {selectedCell.generation}{" "}
                <span className="attempt-grid__detail-status">{selectedCell.status.label}</span>
              </h3>
              {selectedCell.notes.length > 0 && (
                <ul className="attempt-grid__notes">
                  {selectedCell.notes.map((note, index) => (
                    <NoteChip key={`${note.kind}-${index}`} note={note} />
                  ))}
                </ul>
              )}
              {selectedCell.sessions.length === 0 ? (
                <p className="attempt-grid__empty">
                  No session recorded for this generation yet.
                </p>
              ) : (
                <ul className="attempt-grid__sessions">
                  {selectedCell.sessions.map((session) => (
                    <SessionRow
                      key={session.session_id}
                      session={session}
                      onOpen={
                        onOpenSession
                          ? () => onOpenSession(selectedCell.groupId, session.session_id)
                          : undefined
                      }
                    />
                  ))}
                </ul>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}

export default AttemptGrid;
