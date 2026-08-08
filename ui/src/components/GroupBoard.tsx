// The live board (plan U5): one card per snapshot group, laid out in
// topological lanes, with the snapshot's DAG edges drawn as an SVG overlay
// between cards. Re-renders purely off `useRunStream`'s snapshot/revision —
// no fetching of its own, no fixed-interval polling.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { GroupState, RunSnapshot, SnapshotGroup } from "../types";
import "./GroupBoard.css";

const STATE_LABELS: Record<GroupState, string> = {
  pending: "Pending",
  ready: "Ready",
  running: "Running",
  reviewing: "Reviewing",
  rewriting: "Rewriting",
  merging: "Merging",
  completed: "Completed",
  failed: "Failed",
  resolved: "Resolved",
  interrupted: "Interrupted",
};

export interface GroupBoardProps {
  project: string;
  runId: string;
  snapshot: RunSnapshot | null;
  /** Advances on every successful snapshot load — edges re-measure off this. */
  revision: number;
  loading: boolean;
}

interface EdgeLine {
  id: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

/** Topological lanes: a group sits one lane right of its deepest dependency,
 * so every drawn edge points left-to-right. Defensive against a cyclic DAG
 * file — a cycle member gets depth 0 rather than hanging the board. */
function laneLayout(groups: SnapshotGroup[]): SnapshotGroup[][] {
  const deps = new Map<string, string[]>(groups.map((g) => [g.group_id, g.depends_on]));
  const depths = new Map<string, number>();
  const visiting = new Set<string>();

  const depthOf = (id: string): number => {
    const known = depths.get(id);
    if (known !== undefined) return known;
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const parents = (deps.get(id) ?? []).filter((dep) => deps.has(dep));
    const value = parents.length === 0 ? 0 : Math.max(...parents.map(depthOf)) + 1;
    visiting.delete(id);
    depths.set(id, value);
    return value;
  };

  const lanes: SnapshotGroup[][] = [];
  const ordered = [...groups].sort((a, b) => a.group_id.localeCompare(b.group_id));
  for (const group of ordered) {
    const lane = depthOf(group.group_id);
    (lanes[lane] ??= []).push(group);
  }
  return lanes.filter((lane) => lane !== undefined);
}

function curveOf(line: EdgeLine): string {
  const bend = Math.max(24, (line.x2 - line.x1) / 2);
  return `M ${line.x1} ${line.y1} C ${line.x1 + bend} ${line.y1}, ${line.x2 - bend} ${line.y2}, ${line.x2} ${line.y2}`;
}

function GroupBoard({ snapshot, revision, loading }: GroupBoardProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cardRefs = useRef(new Map<string, HTMLDivElement>());
  const [edgeLines, setEdgeLines] = useState<EdgeLine[]>([]);

  const lanes = useMemo(() => (snapshot ? laneLayout(snapshot.groups) : []), [snapshot]);

  // Measure card positions after layout and turn the snapshot's edges into
  // right-edge → left-edge connector lines relative to the board canvas.
  const measure = useCallback(() => {
    const container = containerRef.current;
    if (!container || !snapshot) {
      setEdgeLines([]);
      return;
    }
    const base = container.getBoundingClientRect();
    const lines: EdgeLine[] = [];
    for (const edge of snapshot.edges) {
      const fromEl = cardRefs.current.get(edge.from);
      const toEl = cardRefs.current.get(edge.to);
      if (!fromEl || !toEl) continue;
      const a = fromEl.getBoundingClientRect();
      const b = toEl.getBoundingClientRect();
      lines.push({
        id: `${edge.from}->${edge.to}`,
        x1: a.right - base.left,
        y1: a.top + a.height / 2 - base.top,
        x2: b.left - base.left,
        y2: b.top + b.height / 2 - base.top,
      });
    }
    setEdgeLines(lines);
  }, [snapshot]);

  useLayoutEffect(() => {
    measure();
  }, [measure, revision]);

  useEffect(() => {
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [measure]);

  if (!snapshot) {
    return (
      <section className="group-board" aria-label="Groups">
        <h2>Groups</h2>
        <p className="group-board__empty">
          {loading ? "Loading snapshot…" : "No snapshot for this run yet."}
        </p>
      </section>
    );
  }

  return (
    <section className="group-board" aria-label="Groups">
      <div className="group-board__header">
        <h2>Groups</h2>
        {snapshot.stale_dag && (
          <span className="group-board__stale" role="status">
            ⚠ DAG may not match this run
          </span>
        )}
      </div>
      {snapshot.groups.length === 0 ? (
        <p className="group-board__empty">This run has no groups.</p>
      ) : (
        <div className="group-board__canvas" ref={containerRef}>
          <svg className="group-board__edges" aria-hidden="true">
            <defs>
              <marker
                id="dag-arrow"
                viewBox="0 0 8 8"
                refX="7"
                refY="4"
                markerWidth="6"
                markerHeight="6"
                orient="auto-start-reverse"
              >
                <path d="M 0 0 L 8 4 L 0 8 z" />
              </marker>
            </defs>
            {edgeLines.map((line) => (
              <path key={line.id} d={curveOf(line)} markerEnd="url(#dag-arrow)" />
            ))}
          </svg>
          <div className="group-board__lanes">
            {lanes.map((lane, index) => (
              <div key={index} className="group-board__lane">
                {lane.map((group) => (
                  <div
                    key={group.group_id}
                    ref={(el) => {
                      if (el) cardRefs.current.set(group.group_id, el);
                      else cardRefs.current.delete(group.group_id);
                    }}
                    className={`group-card group-card--${group.state}`}
                    title={group.summary || undefined}
                  >
                    <div className="group-card__head">
                      <span className="group-card__id">{group.group_id}</span>
                      <span className={`group-card__state group-card__state--${group.state}`}>
                        {STATE_LABELS[group.state]}
                      </span>
                    </div>
                    {group.name && <div className="group-card__name">{group.name}</div>}
                    <div className="group-card__generation">gen {group.generation}</div>
                    {group.depends_on.length > 0 && (
                      <div className="group-card__deps">after {group.depends_on.join(", ")}</div>
                    )}
                    {group.failure && <div className="group-card__failure">{group.failure}</div>}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

export default GroupBoard;
