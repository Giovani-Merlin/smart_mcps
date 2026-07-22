// Stub — the live-board slice (plan U5) replaces this with the real board:
// one card per snapshot group, DAG edges, and the stale-DAG marker. The props
// are the contract App.tsx already honors.

import type { RunSnapshot } from "../types";

export interface GroupBoardProps {
  project: string;
  runId: string;
  snapshot: RunSnapshot | null;
  /** Advances on every successful snapshot load — re-render off this. */
  revision: number;
  loading: boolean;
}

function GroupBoard({ snapshot, loading }: GroupBoardProps) {
  return (
    <section className="panel-stub" aria-label="Groups">
      <h2>Groups</h2>
      <p className="panel-stub__note">
        {loading && !snapshot ? "Loading snapshot…" : "Group board lands with the live-board slice."}
      </p>
    </section>
  );
}

export default GroupBoard;
