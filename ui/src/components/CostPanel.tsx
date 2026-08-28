// The Cost tab: what the grouper predicted, and what the run actually spent —
// as two panels that never touch each other.
//
// **Why two panels and not one chart.** `estimated_tokens` predicts context
// occupancy for a single coder session. The cumulative counters are spend across
// every round and every role. Put them in one bar and the eye computes a ratio
// between them, and that ratio is meaningless — a group can be perfectly
// estimated and still show 8x "over" simply because it ran four review rounds.
// So the prediction panel holds `estimated_tokens` against the coder session's
// `last_context_tokens` (the same quantity, prediction vs outcome) and the spend
// panel holds the four token classes, and no element in this file divides a
// value from one panel by a value from the other. `cost.ts` enforces the same
// separation structurally: the spend derivation is never handed the estimate.
//
// **Numbers, not bars.** Cache read runs ~75:1 over cache creation and ~300:1
// over output on a real run (122.7M / 1.6M / 0.4M on r20260828-090936), so a
// proportional bar renders every segment but one as a sliver readable only on
// hover. Each token class is a plain formatted number instead, with cache read
// still visually muted — it is the cheap class, and a session that is mostly
// cache read is healthy, not expensive. The emphasis is chosen in `cost.ts`,
// never here; this file only renders it.
//
// **Absence is a rendering, not an error.** Every run written before the
// token-class split has all four counters at zero, which is a different claim
// from "spent nothing" — those runs show the estimate alone, an explicit
// "actuals not recorded for this run" note, and a `PathChip` to the manifest the
// operator will want to open next. Likewise a group with no recorded
// `intensity`: the expected reviewer-session count reads "unknown" rather than
// assuming a default.

import { useEffect, useMemo, useState } from "react";

import { errorMessage, getRunPaths } from "../api";
import {
  TOKEN_CLASSES,
  billableish,
  buildCostView,
  describeRatio,
  formatTokens,
} from "../cost";
import type { CostView, GroupCost, RoleCost, SessionCost, TokenClasses } from "../cost";
import type { RunSnapshot } from "../types";
import PathChip from "./PathChip";
import "./CostPanel.css";

export interface CostPanelProps {
  project: string;
  runId: string;
  snapshot: RunSnapshot | null;
}

const NO_ACTUALS_NOTE = "actuals not recorded for this run";

// ------------------------------------------------------------------- figures

/** The four token classes as plain formatted numbers (W9). The `data-tokens` /
 * `data-emphasis` attributes stay on the value element so tests keep asserting
 * on values rather than layout. */
function ClassFigures({ classes, testId }: { classes: TokenClasses; testId: string }) {
  return (
    <dl className="cost-figures" data-testid={testId} aria-label={figuresLabel(classes)}>
      {TOKEN_CLASSES.map((cls) => (
        <div
          key={cls.key}
          className={`cost-figures__item cost-figures__item--${cls.emphasis}`}
          title={cls.hint}
        >
          <dt className="cost-figures__label">{cls.label}</dt>
          <dd
            className={`cost-figures__value cost-figures__value--${cls.emphasis}`}
            data-testid={`${testId}-${cls.key}`}
            data-emphasis={cls.emphasis}
            data-tokens={classes[cls.key]}
            title={`${cls.label}: ${classes[cls.key].toLocaleString()} tokens`}
          >
            {formatTokens(classes[cls.key])}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function figuresLabel(classes: TokenClasses): string {
  return TOKEN_CLASSES.map((cls) => `${cls.label} ${classes[cls.key]}`).join(", ");
}

/** Per-round totals, as numbers. Only rendered when the manifest carried a
 * `rounds` list — the usual rendering is nothing at all rather than a single
 * figure implying one round. */
function RoundTotals({ rounds }: { rounds: number[] }) {
  if (rounds.length < 2) return null;
  return (
    <span
      className="cost-rounds"
      data-testid="cost-spark"
      title={`per-round totals: ${rounds.map((r) => r.toLocaleString()).join(", ")}`}
    >
      rounds: {rounds.map((r) => formatTokens(r)).join(" → ")}
    </span>
  );
}

function SessionRow({ session }: { session: SessionCost }) {
  return (
    <li className="cost-session">
      <span className="cost-session__name" title={session.model ?? undefined}>
        gen {session.generation} · {session.name || session.sessionId.slice(0, 8)}
      </span>
      <ClassFigures classes={session.classes} testId={`cost-bar-${session.sessionId}`} />
      <span className="cost-session__total">{formatTokens(session.total)} total</span>
      {session.baseContextTokens > 0 && (
        <span
          className="cost-session__inherited"
          data-testid={`cost-inherited-cache-${session.sessionId}`}
          title="The context this session started from: its first turn's cache read + cache creation — the prefix it inherited and cannot shrink. Near-constant across a run's forks."
        >
          {formatTokens(session.baseContextTokens)} base context
        </span>
      )}
      <RoundTotals rounds={session.roundTotals} />
    </li>
  );
}

function RoleBlock({ groupId, role }: { groupId: string; role: RoleCost }) {
  return (
    <div className="cost-role" data-testid={`cost-role-${groupId}-${role.role}`}>
      <div className="cost-role__head">
        <span className={`cost-role__name cost-role__name--${role.role}`}>{role.role}</span>
        <span className="cost-role__count">
          {role.sessions.length} {role.sessions.length === 1 ? "session" : "sessions"}
        </span>
        <span className="cost-role__total" data-testid={`cost-role-total-${groupId}-${role.role}`}>
          {formatTokens(role.total)} total · {formatTokens(billableish(role.classes))} excluding
          cache reads
        </span>
      </div>
      <ClassFigures classes={role.classes} testId={`cost-role-bar-${groupId}-${role.role}`} />
      {role.sessions.length > 0 && (
        <ul className="cost-role__sessions">
          {role.sessions.map((session) => (
            <SessionRow key={session.sessionId} session={session} />
          ))}
        </ul>
      )}
    </div>
  );
}

// ------------------------------------------------------------ panel: predict

/** Panel one. Same quantity on both sides, and the only ratio in the tab. */
function PredictionPanel({ view, manifestPath }: { view: CostView; manifestPath: string | null }) {
  const { calibration } = view;
  return (
    <section className="cost-panel" aria-label="Prediction vs outcome" data-testid="cost-prediction">
      <div className="cost-panel__head">
        <h3>Prediction vs outcome</h3>
        <PathChip label="groups" path={manifestPath ?? "groups.json (path unavailable)"} />
      </div>
      <p className="cost-panel__note">
        The grouper's <code>estimated_tokens</code> against the coder session's{" "}
        <code>last_context_tokens</code>: context occupancy for one coder, predicted and observed.
        The same quantity on both sides — which is why this is the one comparison in this tab
        expressed as a ratio. Cumulative spend is a different quantity and lives in the panel below.
      </p>

      <div className="cost-calibration" data-testid="cost-calibration">
        <h4>Estimator calibration across the run</h4>
        {calibration.rows.length === 0 ? (
          <p className="cost-panel__empty">
            No group in this run has both an estimate and a recorded coder occupancy, so there is
            nothing to calibrate against.
          </p>
        ) : (
          <>
            <p className="cost-calibration__summary">
              <strong data-testid="cost-calibration-median">
                {calibration.singleGenerationCount === 0
                  ? "no median — no single-generation groups to compute one from"
                  : describeRatio(calibration.medianRatio)}
              </strong>{" "}
              (median across {calibration.singleGenerationCount} single-generation{" "}
              {calibration.singleGenerationCount === 1 ? "group" : "groups"}
              {calibration.multiGenerationCount > 0 &&
                `; ${calibration.multiGenerationCount} multi-generation ${
                  calibration.multiGenerationCount === 1 ? "row" : "rows"
                } excluded`}
              ); summed {formatTokens(calibration.observedTotal)} observed against{" "}
              {formatTokens(calibration.estimatedTotal)} predicted, over that same
              single-generation population. Above 1 means the estimator is under-predicting — the
              lever is <code>bytes_per_token</code> and <code>slack_multiplier</code>.
            </p>
            {calibration.skipped.length > 0 && (
              <p className="cost-panel__note" data-testid="cost-calibration-skipped">
                Not counted: {calibration.skipped.join(", ")} — no estimate or no recorded coder
                occupancy. A calibration figure over an unstated subset is how a tuned-looking
                number gets reported for an untuned estimator.
              </p>
            )}
            <table className="cost-table" data-testid="cost-calibration-rows">
              <thead>
                <tr>
                  <th scope="col">group</th>
                  <th scope="col">generations</th>
                  <th scope="col">last-generation</th>
                  <th scope="col">peak</th>
                </tr>
              </thead>
              <tbody>
                {calibration.rows.map((row) => (
                  <tr
                    key={row.groupId}
                    data-testid={`cost-calibration-row-${row.groupId}`}
                    data-multi-generation={row.multiGeneration}
                  >
                    <th scope="row">
                      <span className="cost-table__id">{row.groupId}</span> {row.name}
                    </th>
                    <td data-testid={`cost-calibration-generations-${row.groupId}`}>
                      {row.generations}
                      {row.multiGeneration && (
                        <span
                          className="cost-calibration__label"
                          data-testid={`cost-calibration-label-${row.groupId}`}
                          title={row.retirementReasons.join("; ")}
                        >
                          {" "}
                          multi-generation — excluded from median; retired:{" "}
                          {row.retirementReasons.join("; ") || "reason not recorded"}
                        </span>
                      )}
                    </td>
                    <td>{describeRatio(row.ratioLast)}</td>
                    <td>{describeRatio(row.ratioPeak)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>

      <table className="cost-table">
        <thead>
          <tr>
            <th scope="col">group</th>
            <th scope="col">predicted context</th>
            <th scope="col">observed context</th>
            <th scope="col">prediction</th>
          </tr>
        </thead>
        <tbody>
          {view.groups.map((group) => (
            <tr key={group.groupId} data-testid={`cost-prediction-${group.groupId}`}>
              <th scope="row">
                <span className="cost-table__id">{group.groupId}</span> {group.name}
              </th>
              <td>{group.prediction.estimated == null ? "—" : formatTokens(group.prediction.estimated)}</td>
              <td>
                {group.prediction.observed == null ? (
                  <span className="cost-table__absent" title={group.prediction.note}>
                    not recorded
                  </span>
                ) : (
                  formatTokens(group.prediction.observed)
                )}
              </td>
              <td title={group.prediction.note}>{describeRatio(group.prediction.ratio)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

// -------------------------------------------------------------- panel: spend

function GroupSpend({ group }: { group: GroupCost }) {
  return (
    <article className="cost-group" data-testid={`cost-group-${group.groupId}`}>
      <header className="cost-group__head">
        <h4>
          <span className="cost-table__id">{group.groupId}</span> {group.name}
        </h4>
        <span
          className={`cost-group__intensity${group.expectation.known ? "" : " cost-group__intensity--unknown"}`}
          data-testid={`cost-intensity-${group.groupId}`}
          title={group.expectation.text}
        >
          {group.expectation.known ? group.intensity : "intensity unknown"}
        </span>
      </header>

      {/* `intensity` decides how many reviewer sessions there should be, so it
          is stated next to the bars rather than left for the operator to infer.
          A self_verify group with zero reviewer sessions is correct, and says
          so; an unknown intensity says that instead of assuming a default. */}
      <p
        className={`cost-group__expectation${group.expectation.asExpected ? " cost-group__expectation--ok" : ""}`}
        data-testid={`cost-expectation-${group.groupId}`}
      >
        {group.expectation.text}
      </p>

      {group.actualsRecorded ? (
        <div className="cost-group__roles">
          {group.roles.map((role) => (
            <RoleBlock key={role.role} groupId={group.groupId} role={role} />
          ))}
        </div>
      ) : (
        <p className="cost-group__absent" data-testid={`cost-absent-${group.groupId}`}>
          {NO_ACTUALS_NOTE}
          {group.prediction.estimated != null && (
            <>
              {" "}
              — the estimate of {formatTokens(group.prediction.estimated)} tokens is all this
              group recorded.
            </>
          )}
        </p>
      )}
    </article>
  );
}

/** Panel two. Cumulative spend only; it is never handed the estimate. */
function SpendPanel({ view, manifestPath }: { view: CostView; manifestPath: string | null }) {
  return (
    <section className="cost-panel" aria-label="Cumulative spend" data-testid="cost-spend">
      <div className="cost-panel__head">
        <h3>Cumulative spend</h3>
        <PathChip label="manifest" path={manifestPath ?? "manifest.json (path unavailable)"} />
      </div>
      <p className="cost-panel__note">
        Every round of every session, by role, in four token classes. Summed from the per-round
        figures the runner records — never from a session-level envelope total, which counts every
        turn and once read 50x high. This is spend, not occupancy: it is not comparable with the
        estimate above and is deliberately not shown against it. Cache read is the cheap class
        (0.1x) and shown dimmed; the loud figures are the ones that actually cost something.
      </p>

      {!view.actualsRecorded && (
        <p className="cost-panel__absent" data-testid="cost-actuals-missing">
          {NO_ACTUALS_NOTE} — this manifest predates the per-session token-class counters, so every
          class reads zero. That is missing bookkeeping, not a run that spent nothing. The estimates
          are shown above on their own.
        </p>
      )}

      {view.groups.length === 0 ? (
        <p className="cost-panel__empty">This run has no groups.</p>
      ) : (
        view.groups.map((group) => <GroupSpend key={group.groupId} group={group} />)
      )}
    </section>
  );
}

// ------------------------------------------------------------------- the tab

export function CostPanel({ project, runId, snapshot }: CostPanelProps) {
  const [manifestPath, setManifestPath] = useState<string | null>(null);
  const [groupsPath, setGroupsPath] = useState<string | null>(null);
  const [pathError, setPathError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const view = await getRunPaths(project, runId);
        if (cancelled) return;
        setManifestPath(view.entries.find((entry) => entry.key === "manifest")?.path ?? null);
        setGroupsPath(view.entries.find((entry) => entry.key === "groups")?.path ?? null);
        setPathError(null);
      } catch (err) {
        if (cancelled) return;
        setManifestPath(null);
        setGroupsPath(null);
        setPathError(errorMessage(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [project, runId]);

  const view = useMemo(() => (snapshot ? buildCostView(snapshot) : null), [snapshot]);

  if (!view) {
    return (
      <section className="cost" aria-label="Cost accounting">
        <p className="cost-panel__empty">Waiting for the run snapshot…</p>
      </section>
    );
  }

  return (
    <section className="cost" aria-label="Cost accounting">
      {pathError && <p className="cost-panel__note">paths unavailable: {pathError}</p>}
      <PredictionPanel view={view} manifestPath={groupsPath ?? manifestPath} />
      <SpendPanel view={view} manifestPath={manifestPath} />
    </section>
  );
}

export default CostPanel;
