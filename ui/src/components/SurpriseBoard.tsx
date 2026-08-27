// The two directions of a group's surprises (plan U12): what is still
// pending on the board addressed to this group, and what this group's own
// coder/reviewer rounds have reported. Rendered as two visually distinct
// sections so neither direction is ever mistaken for the other — a "pending"
// row is something this group still owes a checkpoint, an "emitted" row is
// something this group already told the run.

import type { SnapshotSurprise } from "../types";
import "./SurpriseBoard.css";

function SurpriseRow({ surprise }: { surprise: SnapshotSurprise }) {
  return (
    <li className="surprise-board__row">
      <span className="surprise-board__kind">[{surprise.kind}]</span> {surprise.description}
      {surprise.affected_groups.length > 0 && (
        <span className="surprise-board__muted"> (affects {surprise.affected_groups.join(", ")})</span>
      )}
      {surprise.reason && <span className="surprise-board__reason">{surprise.reason}</span>}
    </li>
  );
}

export interface SurpriseBoardProps {
  /** Surprises still on the board addressed to this group — each carries why
   * it was never delivered (`surprise_residue`, plan U12). */
  pending: SnapshotSurprise[];
  /** Surprises this group's own coder/reviewer rounds reported. */
  emitted: SnapshotSurprise[];
}

function SurpriseBoard({ pending, emitted }: SurpriseBoardProps) {
  if (pending.length === 0 && emitted.length === 0) return null;
  return (
    <div className="surprise-board">
      <div className="surprise-board__section surprise-board__section--pending">
        <h4>Pending for this group</h4>
        {pending.length === 0 ? (
          <p className="drill-in__empty">Nothing pending for this group.</p>
        ) : (
          <ul className="surprise-board__list">
            {pending.map((surprise, index) => (
              <SurpriseRow key={index} surprise={surprise} />
            ))}
          </ul>
        )}
      </div>
      <div className="surprise-board__section surprise-board__section--emitted">
        <h4>Emitted by this group</h4>
        {emitted.length === 0 ? (
          <p className="drill-in__empty">This group has not reported any surprises.</p>
        ) : (
          <ul className="surprise-board__list">
            {emitted.map((surprise, index) => (
              <SurpriseRow key={index} surprise={surprise} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

export default SurpriseBoard;
