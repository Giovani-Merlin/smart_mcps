// Stub — the live-board slice (plan U5) replaces this with the streaming log
// wired to `openLogStream` (`/events/log`). The props are the contract App.tsx
// already honors.

export interface EventLogProps {
  project: string;
  runId: string;
}

function EventLog({ project, runId }: EventLogProps) {
  return (
    <section className="panel-stub" aria-label="Event log">
      <h2>Event log</h2>
      <p className="panel-stub__note">
        Live event log for {project}/{runId} lands with the live-board slice.
      </p>
    </section>
  );
}

export default EventLog;
