// A determinate progress bar for a `group` job (plan U24).
//
// `spec i/N` is the only long, countable stage a grouping job has — the mapper
// and the graph/partition stages are each one short line with no meaningful
// sub-progress. So the bar tracks the *latest* `spec i/N` line seen in the job
// log and renders nothing at all when no such line has arrived yet: a job that
// never emits a recognisable progress line (an older run, a different job
// kind, or one that has not reached the specs stage yet) falls back to
// whatever `JobLog` already renders instead of a bar stuck at 0%.

import { useEffect, useState } from "react";

import "./JobProgress.css";

export interface JobProgressProps {
  lines: string[];
  running: boolean;
  startedAt?: string | null;
}

interface SpecProgress {
  current: number;
  total: number;
}

const SPEC_LINE_RE = /\bspec (\d+)\/(\d+)\b/;

/** The most recent `spec i/N` line in the log, or null if none has arrived —
 * later lines override earlier ones, matching how the job actually advances. */
export function latestSpecProgress(lines: string[]): SpecProgress | null {
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const match = SPEC_LINE_RE.exec(lines[i]);
    if (!match) continue;
    const total = Number(match[2]);
    if (total <= 0) continue;
    const current = Number(match[1]);
    return { current, total };
  }
  return null;
}

/** `3m 05s` / `45s` — the same shape `UsageLimitBanner.formatCountdown` uses,
 * so an elapsed timer and a countdown never read inconsistently side by side. */
export function formatElapsed(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes <= 0) return `${seconds}s`;
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}

export function JobProgress({ lines, running, startedAt }: JobProgressProps) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);

  const progress = latestSpecProgress(lines);
  if (!progress) return null;

  const percent = Math.min(100, Math.round((progress.current / progress.total) * 100));
  const elapsedMs = startedAt ? now - new Date(startedAt).getTime() : null;

  return (
    <div className="job-progress" role="status" aria-label="Job progress">
      <div className="job-progress__track">
        <div className="job-progress__fill" style={{ width: `${percent}%` }} />
      </div>
      <span className="job-progress__label">
        spec {progress.current}/{progress.total} · {percent}%
        {elapsedMs !== null && elapsedMs >= 0 && ` · ${formatElapsed(elapsedMs)} elapsed`}
      </span>
    </div>
  );
}

export default JobProgress;
