import type { GroupState, RunState } from "../types";

const STATE_LABELS: Record<GroupState, string> = {
  pending: "Pending",
  ready: "Ready",
  running: "Running",
  reviewing: "Reviewing",
  rewriting: "Rewriting",
  merging: "Merging",
  completed: "Completed",
  failed: "Failed",
};

interface GroupBoardProps {
  runState: RunState;
}

function GroupBoard({ runState }: GroupBoardProps) {
  const groupIds = Object.keys(runState.groups).sort();

  return (
    <section className="group-board">
      <h2>Groups</h2>
      <div className="group-board__grid">
        {groupIds.map((groupId) => {
          const group = runState.groups[groupId];
          return (
            <div key={groupId} className={`group-card group-card--${group.state}`}>
              <div className="group-card__id">{groupId}</div>
              <div className="group-card__state">{STATE_LABELS[group.state]}</div>
              <div className="group-card__generation">gen {group.generation}</div>
              {group.failure && <div className="group-card__failure">{group.failure}</div>}
            </div>
          );
        })}
      </div>
    </section>
  );
}

export default GroupBoard;
