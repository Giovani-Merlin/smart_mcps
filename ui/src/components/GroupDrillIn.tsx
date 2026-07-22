// Stub — the drill-in slice group implements this pane (plan U9). The props
// below are its contract: the active selection, the composed snapshot (whose
// groups carry the sessions to list), and `useRunStream`'s revision. Backend
// access must go through `api.ts` (`getTranscript`, `getArtifacts`).

import type { RunSnapshot } from "../types";

export interface GroupDrillInProps {
  project: string;
  runId: string;
  /** Group → sessions come from here; null until the first snapshot loads. */
  snapshot: RunSnapshot | null;
  /** Advances on every run-directory change. */
  revision: number;
}

function GroupDrillIn({ project, runId }: GroupDrillInProps) {
  return (
    <section className="panel-stub" aria-label="Group drill-in">
      <h2>Group drill-in</h2>
      <p className="panel-stub__note">
        Transcript and artifact pane for {project}/{runId} lands with the drill-in slice.
      </p>
    </section>
  );
}

export default GroupDrillIn;
