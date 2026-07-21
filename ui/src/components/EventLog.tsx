import type { Escalation } from "../types";

interface EventLogProps {
  lines: string[];
  escalations: Escalation[];
}

function EventLog({ lines, escalations }: EventLogProps) {
  return (
    <section className="event-log">
      <h2>Event log</h2>
      <pre className="event-log__lines">{lines.join("\n")}</pre>

      {escalations.length > 0 && (
        <div className="escalations">
          <h2>Pending escalations</h2>
          <ul className="escalations__list">
            {escalations.map((escalation) => (
              <li key={escalation.id} className="escalation">
                <div className="escalation__meta">
                  <span className="escalation__kind">{escalation.kind}</span>
                  <span className="escalation__group">{escalation.group_id}</span>
                </div>
                <p className="escalation__prompt">{escalation.prompt}</p>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

export default EventLog;
