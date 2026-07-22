// Subscribes to a run's change stream and keeps the composed snapshot fresh.
//
// The backend's `/events/run` carries only a `changed` nudge, never a payload —
// the snapshot endpoint stays the single composition point. So this hook fetches
// the snapshot once on mount, then re-fetches it each time `changed` fires, and
// exposes a monotonic `revision` counter that advances on every successful load.
// Slice components depend on `revision` to re-render off live change rather than
// polling on a fixed interval.

import { useEffect, useState } from "react";

import { errorMessage, getSnapshot, openRunStream } from "./api";
import type { RunSnapshot } from "./types";

export interface RunStream {
  snapshot: RunSnapshot | null;
  /** Bumped on every successful snapshot load — components watch it to re-render. */
  revision: number;
  error: string | null;
  loading: boolean;
}

export function useRunStream(project: string | null, run: string | null): RunStream {
  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!project || !run) {
      setSnapshot(null);
      setRevision(0);
      setError(null);
      setLoading(false);
      return;
    }

    // Capture the narrowed values so the async closure keeps the non-null types.
    const activeProject = project;
    const activeRun = run;
    let cancelled = false;
    setLoading(true);

    async function refresh(): Promise<void> {
      try {
        const next = await getSnapshot(activeProject, activeRun);
        if (cancelled) return;
        setSnapshot(next);
        setRevision((current) => current + 1);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(errorMessage(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void refresh();
    // Every `changed` event re-composes the snapshot; the EventSource itself
    // reconnects on transport drops, so no manual retry is needed here.
    const source = openRunStream(activeProject, activeRun, () => void refresh());

    return () => {
      cancelled = true;
      source.close();
    };
  }, [project, run]);

  return { snapshot, revision, error, loading };
}
