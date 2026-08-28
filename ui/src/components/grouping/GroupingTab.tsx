// The Grouping tab: how this run's plan became groups.
//
// The whole tab renders against data that has been sitting in
// `grouping-trace.json` since the trace schema shipped and was displayed
// nowhere. Nothing here waits on the edge-provenance instrumentation; when that
// artifact appears, the "why is this edge here" panel fills in and the
// degradation notice disappears on its own.
//
// Two things it refuses to do:
//
// **Never render an absence as emptiness.** A missing trace, a missing
// `edge-provenance.json`, a schema version this build does not know — each
// names the artifact and shows a `PathChip` to where it was looked for, because
// the operator's next move is to go read that path.
//
// **Never present a stored record as a paraphrase.** Merge reasons, hub roles
// and flags are shown as the trace spelled them. A prettier wording would be a
// second source of truth for something the grouper already decided.
//
// View state lives in query params (`?stage=`, `?edge=`, `?group=`), object
// identity in path segments — so a link to "the merge stage, this edge
// selected" is a link, not a description of where to click.

import { Suspense, lazy, useEffect, useMemo, useState } from "react";

import { errorMessage, getGrouping, getLlmCall, getLlmCalls } from "../../api";
import { TranscriptEntryView } from "../GroupDrillIn";
import type {
  GroupingView,
  LlmCallDetail,
  LlmCallsView,
  MissingArtifact,
  TranscriptEvent,
} from "../../types";
import PathChip from "../PathChip";
import {
  buildFrames,
  buildPalette,
  describeStage,
  membersOf,
  rankedEdges,
  stageIndexOf,
} from "./stages";
import "./GroupingTab.css";

// The one import of the graph libraries in the whole app, and it is lazy:
// `@xyflow/react` and `@dagrejs/dagre` are ~200kB together and every other
// route would otherwise pay for them.
const GroupingGraph = lazy(() => import("./GroupingGraph"));

const PALETTE_SIZE = 8;

export interface GroupingTabProps {
  project: string;
  runId: string;
  /** Query-param view state, owned by the router. */
  params: URLSearchParams;
  onParamsChange: (next: URLSearchParams) => void;
}

export function GroupingTab({ project, runId, params, onParamsChange }: GroupingTabProps) {
  const [view, setView] = useState<GroupingView | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setView(null);
    setError(null);
    void (async () => {
      try {
        const next = await getGrouping(project, runId);
        if (!cancelled) setView(next);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [project, runId]);

  const frames = useMemo(() => (view ? buildFrames(view) : []), [view]);
  const palette = useMemo(() => buildPalette(frames, PALETTE_SIZE), [frames]);

  const stageIndex = stageIndexOf(frames, params.get("stage"));
  const frame = stageIndex >= 0 ? frames[stageIndex] : null;
  const selectedEdge = params.get("edge");
  const selectedNode = params.get("group");

  function setParam(key: string, value: string | null): void {
    const next = new URLSearchParams(params);
    if (value === null) next.delete(key);
    else next.set(key, value);
    onParamsChange(next);
  }

  if (error) {
    return (
      <section className="grouping-tab grouping-tab--error">
        <h2>Grouping</h2>
        <p className="grouping-degraded__body">{error}</p>
      </section>
    );
  }
  if (!view) {
    return (
      <section className="grouping-tab">
        <h2>Grouping</h2>
        <p className="grouping-muted">Reading the grouping artifacts…</p>
      </section>
    );
  }

  const hasTrace = view.stages.length > 0;

  return (
    <section className="grouping-tab">
      <header className="grouping-header">
        <div>
          <h2>Grouping</h2>
          <p className="grouping-muted">
            How <code>{view.plan_path || "this plan"}</code> became {runId}&rsquo;s groups.
          </p>
        </div>
        <DagSourceChip view={view} />
      </header>

      <PathDrawer paths={view.paths} />

      {view.missing.map((missing) => (
        <Degraded key={missing.artifact} missing={missing} />
      ))}

      {view.failure && (
        <div className="grouping-degraded grouping-degraded--failure">
          <strong>The grouping run failed: {view.failure.kind}</strong>
          <p className="grouping-degraded__body">{view.failure.message}</p>
          <p className="grouping-muted">
            Everything below is what had been recorded when it raised.
          </p>
        </div>
      )}

      <SpeccerCalls project={project} runId={runId} />

      {hasTrace ? (
        <>
          <Stepper
            frames={frames}
            index={stageIndex}
            onSelect={(next) => setParam("stage", frames[next].stage)}
          />

          <div className="grouping-split">
            <Suspense
              fallback={<div className="grouping-graph grouping-graph--loading">Loading graph…</div>}
            >
              {frame && (
                <GroupingGraph
                  view={view}
                  frame={frame}
                  palette={palette}
                  paletteSize={PALETTE_SIZE}
                  selectedEdge={selectedEdge}
                  onSelectEdge={(key) => setParam("edge", key)}
                  selectedNode={selectedNode}
                  onSelectNode={(node) => setParam("group", node)}
                />
              )}
            </Suspense>

            <aside className="grouping-side">
              {frame && (
                <GroupPanel
                  frame={frame}
                  selectedNode={selectedNode}
                  onSelect={(node) => setParam("group", node)}
                />
              )}
              <EdgePanel
                view={view}
                frame={frame}
                selectedEdge={selectedEdge}
                onSelect={(key) => setParam("edge", key)}
              />
            </aside>
          </div>

          <Communities view={view} />
          <SliceAtoms view={view} />
          <Decisions view={view} />
          <HubRoles view={view} />
          <ScorecardPanel view={view} />
          <Difficulty view={view} />
          <Provenance view={view} />
          <Flags view={view} />
        </>
      ) : (
        <p className="grouping-muted">
          Without a trace there is nothing to reconstruct — the sections above name what was
          missing and where it was expected.
        </p>
      )}
    </section>
  );
}

// -------------------------------------------------------------------- header

function DagSourceChip({ view }: { view: GroupingView }) {
  const { dag_source: source } = view;
  const tone =
    source.kind === "run_snapshot" ? "ok" : source.kind === "missing" ? "bad" : "warn";
  return (
    <div className={`grouping-source grouping-source--${tone}`}>
      <span className="grouping-source__kind">{source.kind.replace("_", " ")}</span>
      {source.grouping_name && (
        <span className="grouping-source__name">{source.grouping_name}</span>
      )}
      {source.stale_dag && (
        <span className="grouping-source__stale" title={source.reason}>
          stale_dag
        </span>
      )}
      <p className="grouping-source__reason">{source.reason}</p>
    </div>
  );
}

function PathDrawer({ paths }: { paths: Record<string, string> }) {
  const [open, setOpen] = useState(false);
  const entries = Object.entries(paths);
  if (entries.length === 0) return null;
  return (
    <details className="grouping-paths" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>paths on disk ({entries.length})</summary>
      <ul className="grouping-paths__list">
        {entries.map(([label, path]) => (
          <li key={label}>
            <PathChip label={label.replace("_", " ")} path={path} />
          </li>
        ))}
      </ul>
    </details>
  );
}

/** An absence, stated with the artifact's name and where it was looked for. */
function Degraded({ missing }: { missing: MissingArtifact }) {
  return (
    <div className="grouping-degraded">
      <strong>
        <code>{missing.artifact}</code> is not on disk
      </strong>
      <p className="grouping-degraded__body">{missing.explanation}</p>
      <PathChip label="expected at" path={missing.expected_path} />
    </div>
  );
}

// ------------------------------------------------------------------- stepper

function Stepper({
  frames,
  index,
  onSelect,
}: {
  frames: ReturnType<typeof buildFrames>;
  index: number;
  onSelect: (index: number) => void;
}) {
  const frame = index >= 0 ? frames[index] : null;
  return (
    <div className="grouping-stepper">
      <ol className="grouping-stepper__stages">
        {frames.map((candidate, position) => (
          <li key={candidate.stage}>
            <button
              type="button"
              className={`grouping-stepper__stage${
                position === index ? " grouping-stepper__stage--current" : ""
              }${candidate.moved.size > 0 ? " grouping-stepper__stage--changed" : ""}`}
              onClick={() => onSelect(position)}
              aria-current={position === index}
            >
              <span className="grouping-stepper__name">{candidate.stage}</span>
              <span className="grouping-stepper__count">
                {candidate.moved.size > 0 ? `${candidate.moved.size} moved` : "—"}
              </span>
            </button>
          </li>
        ))}
      </ol>
      <input
        className="grouping-stepper__scrub"
        type="range"
        min={0}
        max={Math.max(frames.length - 1, 0)}
        value={Math.max(index, 0)}
        onChange={(event) => onSelect(Number(event.target.value))}
        aria-label="Pipeline stage"
      />
      {frame && <p className="grouping-muted">{describeStage(frame)}</p>}
    </div>
  );
}

// -------------------------------------------------------------------- panels

function GroupPanel({
  frame,
  selectedNode,
  onSelect,
}: {
  frame: ReturnType<typeof buildFrames>[number];
  selectedNode: string | null;
  onSelect: (node: string | null) => void;
}) {
  const groupIds = [...new Set(Object.values(frame.partition))].sort((a, b) => a - b);
  return (
    <div className="grouping-panel">
      <h3>Groups at {frame.stage}</h3>
      <ul className="grouping-panel__groups">
        {groupIds.map((groupId) => (
          <li key={groupId}>
            <span className="grouping-panel__group-id">group {groupId}</span>
            <ul>
              {membersOf(frame, groupId).map((node) => (
                <li key={node}>
                  <button
                    type="button"
                    className={`grouping-panel__node${
                      frame.moved.has(node) ? " grouping-panel__node--moved" : ""
                    }${selectedNode === node ? " grouping-panel__node--selected" : ""}`}
                    onClick={() => onSelect(selectedNode === node ? null : node)}
                  >
                    {node}
                    {frame.moved.has(node) && (
                      <span className="grouping-panel__moved-tag">moved here</span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </div>
  );
}

function EdgePanel({
  view,
  frame,
  selectedEdge,
  onSelect,
}: {
  view: GroupingView;
  frame: ReturnType<typeof buildFrames>[number] | null;
  selectedEdge: string | null;
  onSelect: (key: string | null) => void;
}) {
  const edges = useMemo(() => rankedEdges(view, frame).slice(0, 40), [view, frame]);
  if (edges.length === 0) return null;

  const provenance = view.edge_provenance;
  return (
    <div className="grouping-panel">
      <h3>Affinity edges</h3>
      <p className="grouping-muted">
        Heaviest first. The weight is the sum of every signal that contributed; which signals
        those were is what <code>edge-provenance.json</code> would carry.
      </p>
      <ul className="grouping-panel__edges">
        {edges.map((edge) => {
          const key = `${edge.from}→${edge.to}`;
          return (
            <li key={key}>
              <button
                type="button"
                className={`grouping-panel__edge${
                  selectedEdge === key ? " grouping-panel__edge--selected" : ""
                }${edge.crossGroup ? " grouping-panel__edge--cut" : ""}`}
                onClick={() => onSelect(selectedEdge === key ? null : key)}
              >
                <span className="grouping-panel__weight">{edge.weight.toFixed(1)}</span>
                <span>{edge.from}</span>
                <span aria-hidden>↔</span>
                <span>{edge.to}</span>
                {edge.crossGroup && <span className="grouping-panel__cut-tag">cut</span>}
              </button>
              {selectedEdge === key && (
                <div className="grouping-panel__evidence">
                  {provenance ? (
                    <pre>{JSON.stringify(provenance[key] ?? provenance, null, 2)}</pre>
                  ) : (
                    <p className="grouping-muted">
                      No per-signal breakdown is recorded for this edge — see the{" "}
                      <code>edge-provenance.json</code> notice above.
                    </p>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function Communities({ view }: { view: GroupingView }) {
  if (view.louvain.length === 0) return null;
  return (
    <div className="grouping-panel">
      <h3>Louvain communities</h3>
      {view.louvain.map((entry, index) => (
        <div key={index}>
          <p className="grouping-muted">
            resolution {entry.resolution} · seed {entry.seed} — the seed is fixed, so this
            partition is reproducible.
          </p>
          <ol className="grouping-panel__communities">
            {entry.communities.map((community, position) => (
              <li key={position}>
                <span className="grouping-panel__group-id">community {position}</span>
                <span>{community.join(", ")}</span>
              </li>
            ))}
          </ol>
        </div>
      ))}
    </div>
  );
}

function SliceAtoms({ view }: { view: GroupingView }) {
  return (
    <div className="grouping-panel">
      <h3>Slice atoms</h3>
      {view.slice_atoms.length === 0 ? (
        <p className="grouping-muted">
          None. The plan declared no vertical slice, so no set of tasks was pinned together.
        </p>
      ) : (
        <ul>
          {view.slice_atoms.map((atom) => (
            <li key={atom.label}>
              <strong>{atom.label}</strong> — {atom.members.join(", ")}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Decisions({ view }: { view: GroupingView }) {
  const accepted = view.merges.filter((merge) => merge.accepted);
  return (
    <div className="grouping-panel">
      <h3>Merge, split and repair decisions</h3>
      <p className="grouping-muted">
        {view.merges.length} merge candidates considered, {accepted.length} accepted ·{" "}
        {view.splits.length} splits · {view.repairs.length} repairs. Reasons are the grouper&rsquo;s
        own, verbatim.
      </p>
      {view.merges.length > 0 && (
        <table className="grouping-table">
          <thead>
            <tr>
              <th>round</th>
              <th>source → target</th>
              <th>edge weight</th>
              <th>merged work</th>
              <th>outcome</th>
            </tr>
          </thead>
          <tbody>
            {view.merges.map((merge, index) => (
              <tr
                key={index}
                className={merge.accepted ? "grouping-table__row--accepted" : undefined}
              >
                <td>{merge.round}</td>
                <td>
                  {merge.source} → {merge.target}
                </td>
                <td>{merge.edge_weight.toFixed(1)}</td>
                <td>{Math.round(merge.merged_work).toLocaleString()}</td>
                <td>
                  <code>{merge.accepted ? "accepted" : merge.reason}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {view.splits.length > 0 && (
        <details>
          <summary>split detail ({view.splits.length})</summary>
          <pre className="grouping-pre">{JSON.stringify(view.splits, null, 2)}</pre>
        </details>
      )}
      {view.repairs.length > 0 && (
        <details>
          <summary>repair detail ({view.repairs.length})</summary>
          <pre className="grouping-pre">{JSON.stringify(view.repairs, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}

function HubRoles({ view }: { view: GroupingView }) {
  if (view.hub_roles.length === 0) return null;
  return (
    <div className="grouping-panel">
      <h3>Hub roles</h3>
      <p className="grouping-muted">
        A task is a hub when its dependency ratio crosses the threshold — that is what decides
        whether it anchors a group or gets pulled into one.
      </p>
      <table className="grouping-table">
        <thead>
          <tr>
            <th>task</th>
            <th>role</th>
            <th>depends on</th>
            <th>depended by</th>
            <th>threshold</th>
          </tr>
        </thead>
        <tbody>
          {view.hub_roles.map((hub) => (
            <tr key={hub.node}>
              <td>{hub.node}</td>
              <td>
                <code>{hub.role}</code>
              </td>
              <td>{hub.depends_on_ratio.toFixed(2)}</td>
              <td>{hub.depended_by_ratio.toFixed(2)}</td>
              <td>{hub.threshold}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ScorecardPanel({ view }: { view: GroupingView }) {
  const card = view.scorecard;
  if (!card) {
    return (
      <div className="grouping-panel">
        <h3>Scorecard</h3>
        <p className="grouping-muted">
          This trace predates the scorecard section — the numbers were never computed for this
          grouping, rather than computed and lost.
        </p>
      </div>
    );
  }
  return (
    <div className="grouping-panel">
      <h3>Scorecard</h3>
      <dl className="grouping-scorecard">
        <Stat label="groups" value={card.group_count} />
        <Stat label="cross-group edges" value={card.cross_group_edges} />
        <Stat label="critical path" value={card.critical_path_length} />
        <Stat label="modularity" value={card.modularity.toFixed(3)} />
        <Stat label="work min" value={pct(card.work_fraction_min)} />
        <Stat label="work mean" value={pct(card.work_fraction_mean)} />
        <Stat label="work max" value={pct(card.work_fraction_max)} />
        <Stat label="slice integrity" value={card.slice_integrity_ok ? "ok" : "violated"} />
      </dl>
    </div>
  );
}

function Difficulty({ view }: { view: GroupingView }) {
  if (view.group_difficulty.length === 0) return null;
  return (
    <div className="grouping-panel">
      <h3>Per-group difficulty and review tier</h3>
      <p className="grouping-muted">
        <code>intensity</code> is what decides how many reviewer sessions a group gets, so a
        self_verify group with none is correct rather than missing data.
      </p>
      <table className="grouping-table">
        <thead>
          <tr>
            <th>group</th>
            <th>difficulty</th>
            <th>intensity</th>
            <th>files</th>
            <th>fan in/out</th>
            <th>hub touches</th>
            <th>cross-group</th>
            <th>verification</th>
          </tr>
        </thead>
        <tbody>
          {view.group_difficulty.map((group) => (
            <tr key={group.group_id}>
              <td>{group.group_id}</td>
              <td>{group.difficulty.toFixed(2)}</td>
              <td>
                <code>{group.intensity}</code>
              </td>
              <td>{group.files_touched}</td>
              <td>
                {group.max_fan_in}/{group.max_fan_out}
              </td>
              <td>{group.hub_touches}</td>
              <td>{group.cross_group_edges}</td>
              <td>{group.verification_items}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Provenance({ view }: { view: GroupingView }) {
  const provenance = view.provenance;
  if (!provenance) return null;
  return (
    <div className="grouping-panel">
      <h3>What this partition can be attributed to</h3>
      <dl className="grouping-scorecard">
        <Stat label="grouped at" value={provenance.timestamp} />
        <Stat label="repo commit" value={provenance.repo_commit_sha.slice(0, 12)} />
        <Stat label="worktree" value={provenance.worktree_dirty ? "dirty" : "clean"} />
        <Stat label="plan sha256" value={provenance.plan_content_sha256.slice(0, 12)} />
        <Stat label="index fingerprint" value={provenance.index_fingerprint.slice(0, 12)} />
      </dl>
      {provenance.worktree_dirty && (
        <p className="grouping-muted">
          The worktree was dirty when this ran, so the commit sha alone does not identify the
          input the grouper actually saw.
        </p>
      )}
    </div>
  );
}

function Flags({ view }: { view: GroupingView }) {
  const groups: Array<[string, string[]]> = [
    ["mapper", view.mapper_flags],
    ["partition", view.partition_flags],
    ["result", view.flags],
  ];
  if (groups.every(([, flags]) => flags.length === 0)) return null;
  return (
    <div className="grouping-panel">
      <h3>Flags</h3>
      {groups
        .filter(([, flags]) => flags.length > 0)
        .map(([label, flags]) => (
          <div key={label}>
            <span className="grouping-panel__group-id">{label}</span>
            <ul>
              {flags.map((flag) => (
                <li key={flag}>
                  <code>{flag}</code>
                </li>
              ))}
            </ul>
          </div>
        ))}
    </div>
  );
}

// ---------------------------------------------------------- speccer sessions

/** A timestamp in the operator's own local zone, with the zone named — matches
 * what `GroupDrillIn` shows for a session's `started_at` (plan U35/F22). */
function formatLocalTimestamp(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return null;
  const zone = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" })
    .formatToParts(when)
    .find((part) => part.type === "timeZoneName")?.value;
  return zone ? `${when.toLocaleString()} ${zone}` : when.toLocaleString();
}

/** The grouper's own LLM runs — mapper and speccer calls already persisted in
 * the grouping directory's `llm/` records (plan U31). Fetched independently of
 * `GroupingView`: the call index lives beside, not inside, the trace, and a
 * trace-less run (or a run whose grouping predates the recorder) can still have
 * one, or vice versa. Each row carries `recorded_at`, so a rewrite speccer call
 * made mid-run (once U14 records one) sits alongside the grouping-time calls,
 * distinguished by when it ran rather than by a separate list. */
function SpeccerCalls({ project, runId }: { project: string; runId: string }) {
  const [view, setView] = useState<LlmCallsView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedSeq, setSelectedSeq] = useState<number | null>(null);
  const [detail, setDetail] = useState<LlmCallDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setView(null);
    setError(null);
    setSelectedSeq(null);
    void (async () => {
      try {
        const next = await getLlmCalls(project, runId);
        if (!cancelled) setView(next);
      } catch (err) {
        if (!cancelled) setError(errorMessage(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [project, runId]);

  useEffect(() => {
    setDetail(null);
    setDetailError(null);
    if (selectedSeq === null) return;
    const activeSeq = selectedSeq;
    let cancelled = false;
    void (async () => {
      try {
        const next = await getLlmCall(project, runId, activeSeq);
        if (!cancelled) setDetail(next);
      } catch (err) {
        if (!cancelled) setDetailError(errorMessage(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [project, runId, selectedSeq]);

  function selectCall(seq: number): void {
    setSelectedSeq((current) => (current === seq ? null : seq));
  }

  return (
    <div className="grouping-panel">
      <h3>Speccer &amp; mapper runs</h3>
      {error ? (
        <p className="grouping-degraded__body">{error}</p>
      ) : !view ? (
        <p className="grouping-muted">Reading the LLM call index…</p>
      ) : !view.present ? (
        <Degraded missing={view.missing[0]} />
      ) : view.calls.length === 0 ? (
        <p className="grouping-muted">
          The call index is present but recorded no attempts for this grouping.
        </p>
      ) : (
        <div className="grouping-split">
          <table className="grouping-table">
            <thead>
              <tr>
                <th>when</th>
                <th>call</th>
                <th>model</th>
                <th>attempt</th>
                <th>status</th>
                <th>tokens in/out</th>
                <th>cache read/creation</th>
              </tr>
            </thead>
            <tbody>
              {view.calls.map((call) => (
                <tr
                  key={call.seq}
                  className={
                    call.seq === selectedSeq ? "grouping-table__row--accepted" : undefined
                  }
                >
                  <td>
                    <button
                      type="button"
                      className="grouping-panel__edge"
                      aria-pressed={call.seq === selectedSeq}
                      onClick={() => selectCall(call.seq)}
                    >
                      {formatLocalTimestamp(call.recorded_at) ?? call.recorded_at}
                    </button>
                  </td>
                  <td>
                    <code>{call["gen_ai.operation.name"]}</code>
                  </td>
                  <td>{call["gen_ai.request.model"] ?? "—"}</td>
                  <td>{call.attempt}</td>
                  <td>
                    <code>{call.status.code}</code>
                  </td>
                  <td>
                    {call["gen_ai.usage.input_tokens"]}/{call["gen_ai.usage.output_tokens"]}
                  </td>
                  <td>
                    {call["claude.usage.cache_read_tokens"]}/
                    {call["claude.usage.cache_creation_tokens"]}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <aside className="grouping-side">
            {selectedSeq === null ? (
              <p className="drill-in__empty">Select a call to read its prompt and response.</p>
            ) : detailError ? (
              <p className="drill-in__error">
                Call {selectedSeq} unavailable: {detailError}
              </p>
            ) : !detail ? (
              <p className="drill-in__empty">Loading call…</p>
            ) : (
              <SpeccerCallViewer detail={detail} />
            )}
          </aside>
        </div>
      )}
    </div>
  );
}

/** One call's prompt and response, rendered in the same session viewer used
 * for coder and reviewer transcripts (`TranscriptEntryView`) — the prompt as a
 * user turn, the raw response as an assistant turn. A grouper call has no
 * `.jsonl` transcript of its own (it is a single request/response, not a
 * multi-turn session), so the two texts already on the call record are
 * wrapped as synthetic events rather than fetched from a session file. */
function SpeccerCallViewer({ detail }: { detail: LlmCallDetail }) {
  const events: TranscriptEvent[] = [];
  if (detail.request_text != null) {
    events.push({
      seq: 1,
      role: "user",
      kind: "text",
      text: detail.request_text,
      is_error: false,
      thinking_withheld: false,
    });
  }
  if (detail.raw_text != null) {
    events.push({
      seq: 2,
      role: "assistant",
      kind: "text",
      text: detail.raw_text,
      is_error: Boolean(detail.call.error),
      model: detail.call["gen_ai.request.model"] ?? null,
      thinking_withheld: false,
    });
  }
  return (
    <div className="drill-in__main">
      <h4>
        {detail.call["gen_ai.operation.name"]} · attempt {detail.call.attempt}
      </h4>
      {detail.missing.map((missing) => (
        <Degraded key={missing.artifact} missing={missing} />
      ))}
      {events.length === 0 ? (
        <p className="drill-in__empty">Neither the prompt nor the response could be read.</p>
      ) : (
        <ol className="drill-in__entries">
          {events.map((event) => (
            <TranscriptEntryView key={event.seq} event={event} />
          ))}
        </ol>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="grouping-scorecard__stat">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function pct(fraction: number): string {
  return `${(fraction * 100).toFixed(1)}%`;
}

export default GroupingTab;
