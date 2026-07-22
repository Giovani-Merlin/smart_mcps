// The live event log (plan U5): renders `run.log` lines from the `/events/log`
// SSE stream in arrival order, appending as they arrive and keeping the pane
// pinned to the newest line. All transport goes through `api.ts`'s
// `openLogStream` — no raw fetch/EventSource here.

import { useEffect, useRef, useState } from "react";

import { openLogStream } from "../api";
import "./EventLog.css";

export interface EventLogProps {
  project: string;
  runId: string;
}

function EventLog({ project, runId }: EventLogProps) {
  const [lines, setLines] = useState<string[]>([]);
  const [stalled, setStalled] = useState(false);
  const paneRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setLines([]);
    setStalled(false);
    const source = openLogStream(
      project,
      runId,
      (line) => {
        setStalled(false);
        setLines((prev) => [...prev, line]);
      },
      {
        onError: () => {
          // The EventSource reconnects on its own, and on reconnect the
          // backend replays the whole backlog — reset so nothing duplicates.
          setStalled(true);
          setLines([]);
        },
      },
    );
    return () => source.close();
  }, [project, runId]);

  // Pinned to the newest line: every append scrolls the pane to the bottom.
  useEffect(() => {
    const pane = paneRef.current;
    if (pane) pane.scrollTop = pane.scrollHeight;
  }, [lines]);

  return (
    <section className="event-log" aria-label="Event log">
      <div className="event-log__header">
        <h2>Event log</h2>
        {stalled && (
          <span className="event-log__stalled" role="status">
            stream interrupted — reconnecting…
          </span>
        )}
      </div>
      <div className="event-log__pane" ref={paneRef}>
        {lines.length === 0 ? (
          <p className="event-log__empty">Waiting for run.log lines…</p>
        ) : (
          <ol className="event-log__lines">
            {lines.map((line, index) => (
              <li key={index} className="event-log__line">
                {line}
              </li>
            ))}
          </ol>
        )}
      </div>
    </section>
  );
}

export default EventLog;
