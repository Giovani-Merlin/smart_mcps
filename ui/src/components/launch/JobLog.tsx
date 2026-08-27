// A launched job's log, streamed live.
//
// The same line-at-a-time shape as `EventLog`, against `/events/job` instead of
// `/events/log`. It is a separate component rather than a prop on that one
// because it answers a different question at a different moment: `EventLog`
// tails a *run* that already has a directory, while this tails a job that may
// not have produced a run at all yet — grouping a plan is the case that
// motivated it, and it is watchable here from its first line.

import { useEffect, useRef, useState } from "react";

import { getJob, openJobStream } from "../../api";
import type { JobInfo } from "../../types";
import "../EventLog.css";

export interface JobLogProps {
  project: string;
  job: JobInfo | null;
}

// How often a still-running job's status is re-checked. `job.running` is
// frozen at whatever it was when the caller last handed this component a
// `JobInfo` — a job POSTed once and never refetched reads as "running"
// forever, even after it exits (F6). Polling stops once the job is no longer
// running, so a finished job costs nothing further.
const JOB_STATUS_POLL_MS = 3000;

export function JobLog({ project, job }: JobLogProps) {
  const [lines, setLines] = useState<string[]>([]);
  const [stalled, setStalled] = useState(false);
  const [running, setRunning] = useState(job?.running ?? false);
  const paneRef = useRef<HTMLDivElement | null>(null);
  const jobId = job?.job_id ?? "";

  useEffect(() => {
    setRunning(job?.running ?? false);
  }, [jobId, job?.running]);

  useEffect(() => {
    if (!jobId || !running) return;
    let cancelled = false;
    const timer = window.setInterval(async () => {
      try {
        const next = await getJob(project, jobId);
        if (!cancelled) setRunning(next.running);
      } catch {
        // A failed poll leaves the last known status on screen rather than
        // flipping the badge on a transient network error.
      }
    }, JOB_STATUS_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [project, jobId, running]);

  useEffect(() => {
    setLines([]);
    setStalled(false);
    if (!jobId) return;
    const source = openJobStream(
      project,
      jobId,
      (line) => {
        setStalled(false);
        setLines((prev) => [...prev, line]);
      },
      {
        // The EventSource reconnects itself and the backend replays the whole
        // backlog on reconnect, so the buffer is cleared rather than appended
        // to — otherwise every reconnect duplicates the log so far.
        onError: () => {
          setStalled(true);
          setLines([]);
        },
      },
    );
    return () => source.close();
  }, [project, jobId]);

  useEffect(() => {
    const pane = paneRef.current;
    if (pane) pane.scrollTop = pane.scrollHeight;
  }, [lines]);

  if (!job) return null;

  return (
    <section className="event-log" aria-label="Job log">
      <div className="event-log__header">
        <h3>
          {job.kind} · {job.job_id}
        </h3>
        <span className="event-log__stalled" role="status">
          {stalled ? "stream interrupted — reconnecting…" : running ? "running" : "exited"}
        </span>
      </div>
      <div className="event-log__pane" ref={paneRef}>
        {lines.length === 0 ? (
          <p className="event-log__empty">Waiting for output…</p>
        ) : (
          <ul className="event-log__lines">
            {lines.map((line, index) => (
              <li className="event-log__line" key={`${index}-${line}`}>
                {line}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

export default JobLog;
