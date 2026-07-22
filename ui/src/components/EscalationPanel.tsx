// Stub — the hitl slice group implements this panel (plan U7). The props below
// are its contract: the active selection plus `useRunStream`'s revision, which
// that group uses to clear resolved entries when the run directory changes.
// Backend access must go through `api.ts` (`listEscalations`, `answerEscalation`).

export interface EscalationPanelProps {
  project: string;
  runId: string;
  /** Advances on every run-directory change; refresh the pending list off it. */
  revision: number;
}

function EscalationPanel({ project, runId }: EscalationPanelProps) {
  return (
    <section className="panel-stub" aria-label="Escalations">
      <h2>Escalations</h2>
      <p className="panel-stub__note">
        Pending-escalation panel for {project}/{runId} lands with the hitl slice.
      </p>
    </section>
  );
}

export default EscalationPanel;
