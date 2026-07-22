// The per-group drill-in pane (plan U9): pick a group, see its sessions from
// the snapshot with role and generation, read any session's transcript, and
// review that group's report / verdict artifacts. Transcripts are the one
// polled surface — the backend re-reads the file on every call — so the pane
// re-fetches on an interval only while a session is selected, and the effect
// cleanup stops the timer the moment the pane closes, the selection changes,
// or the component unmounts. A leaked interval against a live run would hit
// the server forever, so that teardown is part of the contract. All backend
// access goes through `api.ts` (`getTranscript`, `getArtifacts`) — never raw
// fetch.

import { useEffect, useState } from "react";

import { errorMessage, getArtifacts, getTranscript } from "../api";
import type {
  Artifact,
  CoderReport,
  ReviewerVerdict,
  RunSnapshot,
  SnapshotGroup,
  Surprise,
  TranscriptEvent,
} from "../types";
import "./GroupDrillIn.css";

/** How often an open transcript re-fetches. The backend re-reads the file per
 * call, so this is what makes a live session's pane advance. */
const TRANSCRIPT_POLL_MS = 3000;

function formatJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

// -------------------------------------------------------------- transcript

/** One normalized transcript moment. The three renderable kinds — assistant
 * text, tool call (name + input), tool result — each get a distinct visual
 * treatment so a human can follow what the agent actually did. */
function TranscriptEntryView({ event }: { event: TranscriptEvent }) {
  if (event.kind === "tool_use") {
    return (
      <li className="transcript-entry transcript-entry--tool-use">
        <div className="transcript-entry__head">
          <span className="transcript-entry__badge">tool call</span>
          <span className="transcript-entry__tool-name">{event.tool_name ?? "unknown tool"}</span>
        </div>
        {event.tool_input != null && (
          <pre className="transcript-entry__code">{formatJson(event.tool_input)}</pre>
        )}
      </li>
    );
  }
  if (event.kind === "tool_result") {
    return (
      <li
        className={`transcript-entry transcript-entry--tool-result${
          event.is_error ? " transcript-entry--error" : ""
        }`}
      >
        <div className="transcript-entry__head">
          <span className="transcript-entry__badge">{event.is_error ? "tool error" : "tool result"}</span>
        </div>
        {event.tool_result && <pre className="transcript-entry__code">{event.tool_result}</pre>}
      </li>
    );
  }
  // kind === "text" (and any future kind the backend normalizes to text-like).
  return (
    <li className={`transcript-entry transcript-entry--text transcript-entry--${event.role}`}>
      <div className="transcript-entry__head">
        <span className="transcript-entry__badge">{event.role}</span>
      </div>
      <p className="transcript-entry__text">{event.text ?? ""}</p>
    </li>
  );
}

// --------------------------------------------------------------- artifacts

function SurpriseList({ surprises }: { surprises?: Surprise[] }) {
  if (!surprises || surprises.length === 0) return null;
  return (
    <div className="artifact-card__section">
      <span className="artifact-card__label">surprises</span>
      <ul className="artifact-card__surprises">
        {surprises.map((surprise, index) => (
          <li key={index}>
            <span className="artifact-card__mono">[{surprise.kind}]</span> {surprise.description}
            {(surprise.affected_groups ?? []).length > 0 && (
              <span className="artifact-card__muted">
                {" "}
                (affects {surprise.affected_groups.join(", ")})
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Parsed `report-*.json` content. The cast is defensive — `content` is
 * whatever JSON was on disk — so every field renders behind a presence check. */
function ReportBody({ content }: { content: unknown }) {
  const report = (content ?? {}) as Partial<CoderReport>;
  return (
    <div className="artifact-card__body">
      {report.status && (
        <span className={`artifact-card__status artifact-card__status--${report.status}`}>
          {report.status}
        </span>
      )}
      {report.summary && <p className="artifact-card__summary">{report.summary}</p>}
      {report.question && (
        <div className="artifact-card__section">
          <span className="artifact-card__label">question</span>
          <p className="artifact-card__summary">{report.question}</p>
        </div>
      )}
      {report.verification_results && report.verification_results.length > 0 && (
        <div className="artifact-card__section">
          <span className="artifact-card__label">verification</span>
          <table className="artifact-card__table">
            <tbody>
              {report.verification_results.map((result, index) => (
                <tr key={index}>
                  <td className="artifact-card__mono">{result.item_id}</td>
                  <td>
                    <span className={`artifact-card__check artifact-card__check--${result.status}`}>
                      {result.status}
                    </span>
                  </td>
                  <td className="artifact-card__muted">{result.notes}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <SurpriseList surprises={report.surprises} />
    </div>
  );
}

/** Parsed `verdict-*.json` content, same defensive posture as ReportBody. */
function VerdictBody({ content }: { content: unknown }) {
  const verdict = (content ?? {}) as Partial<ReviewerVerdict>;
  return (
    <div className="artifact-card__body">
      {verdict.status && (
        <span className={`artifact-card__status artifact-card__status--${verdict.status}`}>
          {verdict.status}
        </span>
      )}
      {verdict.notes && <p className="artifact-card__summary">{verdict.notes}</p>}
      {verdict.required_changes && verdict.required_changes.length > 0 && (
        <div className="artifact-card__section">
          <span className="artifact-card__label">required changes</span>
          <ul className="artifact-card__changes">
            {verdict.required_changes.map((change, index) => (
              <li key={index}>{change}</li>
            ))}
          </ul>
        </div>
      )}
      <SurpriseList surprises={verdict.surprises} />
    </div>
  );
}

function ArtifactCard({ artifact }: { artifact: Artifact }) {
  return (
    <li className={`artifact-card artifact-card--${artifact.kind}`}>
      <div className="artifact-card__head">
        <span className={`artifact-card__kind artifact-card__kind--${artifact.kind}`}>
          {artifact.kind}
        </span>
        <span className="artifact-card__name">{artifact.name}</span>
      </div>
      {artifact.error ? (
        <p className="drill-in__error">{artifact.error}</p>
      ) : artifact.kind === "report" ? (
        <ReportBody content={artifact.content} />
      ) : artifact.kind === "verdict" ? (
        <VerdictBody content={artifact.content} />
      ) : (
        <pre className="transcript-entry__code">{formatJson(artifact.content)}</pre>
      )}
    </li>
  );
}

// -------------------------------------------------------------------- pane

export interface GroupDrillInProps {
  project: string;
  runId: string;
  /** Group → sessions come from here; null until the first snapshot loads. */
  snapshot: RunSnapshot | null;
  /** Advances on every run-directory change. */
  revision: number;
}

function GroupDrillIn({ project, runId, snapshot, revision }: GroupDrillInProps) {
  const [groupId, setGroupId] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TranscriptEvent[] | null>(null);
  const [transcriptError, setTranscriptError] = useState<string | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[] | null>(null);
  const [artifactsError, setArtifactsError] = useState<string | null>(null);

  // Run switch: the previous run's selection must not linger (its group and
  // session ids mean nothing in the new run).
  useEffect(() => {
    setGroupId(null);
    setSessionId(null);
  }, [project, runId]);

  // Group switch (or close): drop the previous group's artifacts immediately
  // rather than showing them under the new group's header.
  useEffect(() => {
    setArtifacts(null);
    setArtifactsError(null);
  }, [project, runId, groupId]);

  // Artifacts refresh off the run-change `revision`, never on a fixed interval
  // — reports and verdicts only change when the run directory does.
  useEffect(() => {
    if (!groupId) return;
    const activeGroup = groupId;
    let cancelled = false;
    void (async () => {
      try {
        const next = await getArtifacts(project, runId, activeGroup);
        if (cancelled) return;
        setArtifacts(next);
        setArtifactsError(null);
      } catch (err) {
        if (cancelled) return;
        setArtifactsError(errorMessage(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [project, runId, groupId, revision]);

  // The poll-while-open contract: this interval exists only while a session is
  // selected. The cleanup runs when the pane closes, the selected session
  // changes, or the component unmounts — after it, no further requests fire.
  useEffect(() => {
    setTranscript(null);
    setTranscriptError(null);
    if (!sessionId) return;

    // Capture the narrowed value so the async closure keeps the non-null type.
    const activeSession = sessionId;
    let cancelled = false;
    let inflight = false;

    async function poll(): Promise<void> {
      if (inflight) return; // a slow response outlives the tick — skip, never stack
      inflight = true;
      try {
        const events = await getTranscript(project, runId, activeSession);
        if (!cancelled) {
          setTranscript(events);
          setTranscriptError(null);
        }
      } catch (err) {
        // A 404 (null transcript_path, or the file not written yet) surfaces
        // as a message; polling continues so a live session appears once its
        // transcript exists on disk.
        if (!cancelled) setTranscriptError(errorMessage(err));
      } finally {
        inflight = false;
      }
    }

    void poll();
    const timer = window.setInterval(() => void poll(), TRANSCRIPT_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [project, runId, sessionId]);

  function selectGroup(next: string): void {
    // Clicking the active group again closes the pane — which also stops the
    // transcript poll via the session reset below.
    setGroupId((current) => (current === next ? null : next));
    setSessionId(null);
  }

  function selectSession(next: string): void {
    setSessionId((current) => (current === next ? null : next));
  }

  function closePane(): void {
    setGroupId(null);
    setSessionId(null);
  }

  const group: SnapshotGroup | null =
    (groupId && snapshot?.groups.find((g) => g.group_id === groupId)) || null;

  return (
    <section className="drill-in" aria-label="Group drill-in">
      <h2>Group drill-in</h2>

      {!snapshot ? (
        <p className="drill-in__empty">Waiting for the run snapshot…</p>
      ) : snapshot.groups.length === 0 ? (
        <p className="drill-in__empty">This run has no groups to inspect.</p>
      ) : (
        <>
          <div className="drill-in__groups" role="group" aria-label="Select a group">
            {snapshot.groups.map((g) => (
              <button
                key={g.group_id}
                type="button"
                className={`drill-in__group-chip${
                  g.group_id === groupId ? " drill-in__group-chip--active" : ""
                }`}
                aria-pressed={g.group_id === groupId}
                title={g.summary || undefined}
                onClick={() => selectGroup(g.group_id)}
              >
                <span className="drill-in__group-chip-id">{g.group_id}</span>
                {g.name && <span className="drill-in__group-chip-name">{g.name}</span>}
              </button>
            ))}
          </div>

          {groupId === null ? (
            <p className="drill-in__empty">
              Select a group to inspect its sessions, transcripts, reports and verdicts.
            </p>
          ) : !group ? (
            <p className="drill-in__empty">
              Group {groupId} is no longer in this run's snapshot.
            </p>
          ) : (
            <div className="drill-in__pane">
              <div className="drill-in__pane-head">
                <h3>
                  {group.group_id}
                  {group.name ? ` — ${group.name}` : ""}
                </h3>
                <button type="button" className="drill-in__close" onClick={closePane}>
                  Close
                </button>
              </div>

              <div className="drill-in__pane-body">
                <div className="drill-in__side">
                  <h4>Sessions</h4>
                  {group.sessions.length === 0 ? (
                    <p className="drill-in__empty">
                      No sessions yet for this group — no agent has started.
                    </p>
                  ) : (
                    <ul className="drill-in__sessions">
                      {group.sessions.map((session) => (
                        <li key={session.session_id}>
                          <button
                            type="button"
                            className={`drill-in__session${
                              session.session_id === sessionId ? " drill-in__session--active" : ""
                            }`}
                            aria-pressed={session.session_id === sessionId}
                            onClick={() => selectSession(session.session_id)}
                          >
                            <span
                              className={`drill-in__session-role drill-in__session-role--${session.role}`}
                            >
                              {session.role}
                            </span>
                            <span className="drill-in__session-gen">gen {session.generation}</span>
                            <span className="drill-in__session-name">{session.name}</span>
                            {session.retirement_reason && (
                              <span className="drill-in__session-retired">
                                retired: {session.retirement_reason}
                              </span>
                            )}
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}

                  <h4>Reports &amp; verdicts</h4>
                  {artifactsError ? (
                    <p className="drill-in__error">{artifactsError}</p>
                  ) : artifacts === null ? (
                    <p className="drill-in__empty">Loading artifacts…</p>
                  ) : artifacts.length === 0 ? (
                    <p className="drill-in__empty">No reports or verdicts yet for this group.</p>
                  ) : (
                    <ul className="drill-in__artifacts">
                      {artifacts.map((artifact) => (
                        <ArtifactCard key={artifact.name} artifact={artifact} />
                      ))}
                    </ul>
                  )}
                </div>

                <div className="drill-in__main">
                  <div className="drill-in__transcript-head">
                    <h4>Transcript</h4>
                    {sessionId && (
                      <span className="drill-in__live-note">
                        refreshes every {TRANSCRIPT_POLL_MS / 1000}s while open
                      </span>
                    )}
                  </div>
                  {!sessionId ? (
                    <p className="drill-in__empty">Select a session to read its transcript.</p>
                  ) : (
                    <>
                      {transcriptError && (
                        <p className="drill-in__error">
                          Transcript unavailable for session {sessionId}: {transcriptError}
                        </p>
                      )}
                      {!transcriptError && transcript === null && (
                        <p className="drill-in__empty">Loading transcript…</p>
                      )}
                      {transcript && transcript.length === 0 && (
                        <p className="drill-in__empty">No renderable transcript events yet.</p>
                      )}
                      {transcript && transcript.length > 0 && (
                        <ol className="drill-in__entries">
                          {transcript.map((event) => (
                            <TranscriptEntryView key={event.seq} event={event} />
                          ))}
                        </ol>
                      )}
                    </>
                  )}
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}

export default GroupDrillIn;
