// The pending-escalations panel (plan U7): the Observatory's single write
// surface. Lists the run's unanswered escalations — full prompt, never
// truncated, plus the request's context pointers — and answers each through
// `api.ts`'s `answerEscalation`, never raw fetch. The underlying protocol is
// file-based and the backend is authoritative, so two behaviors are deliberate:
// a resolved entry is never removed optimistically (it clears only when the
// next run-change `revision` re-fetches the list and the backend no longer
// returns it), and a 409 means someone else — the CLI, or another tab —
// answered first, so the panel says so and refreshes rather than failing
// silently.

import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { ApiError, answerEscalation, errorMessage, listEscalations } from "../api";
import type { EscalationKind, EscalationRequest, HumanAction } from "../types";
import "./EscalationPanel.css";

const KIND_LABELS: Record<EscalationKind, string> = {
  coder_question: "Coder question",
  coder_blocked: "Coder blocked",
  reviewer_too_hard: "Reviewer: too hard",
  reviewer_structural: "Reviewer: structural",
  merge_conflict: "Merge conflict",
  preflight_failed: "Preflight failed",
  caps_exhausted: "Caps exhausted",
  group_resolve: "Resolve stranded work",
  group_start: "Group start",
  respawn: "Respawn",
  merge_approve: "Merge approve",
};

const ACTIONS: readonly HumanAction[] = ["answer", "skip", "abort"];

function formatCreated(iso: string): string {
  const time = new Date(iso);
  return Number.isNaN(time.getTime()) ? iso : time.toLocaleString();
}

// ------------------------------------------------------------------- entry

// idle → inflight → submitted (kept on screen until the next revision drops
// it from the pending list) — or conflict, when the backend 409s because the
// escalation was already answered elsewhere.
type SubmitPhase = "idle" | "inflight" | "submitted" | "conflict";

interface EscalationEntryProps {
  project: string;
  runId: string;
  escalation: EscalationRequest;
  /** Fired on a 409 so the panel can surface the message and refresh the list. */
  onConflict: (message: string) => void;
}

function EscalationEntry({ project, runId, escalation, onConflict }: EscalationEntryProps) {
  const [action, setAction] = useState<HumanAction>("answer");
  const [text, setText] = useState("");
  const [phase, setPhase] = useState<SubmitPhase>("idle");
  const [submitError, setSubmitError] = useState<string | null>(null);

  const ctx = escalation.context;
  const hasContext = Boolean(
    ctx.report_path || ctx.verdict_path || ctx.diff_summary || ctx.surprises.length > 0,
  );
  const resolved = phase === "submitted" || phase === "conflict";

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (phase !== "idle") return;
    setPhase("inflight");
    setSubmitError(null);
    const trimmed = text.trim();
    try {
      await answerEscalation(
        project,
        runId,
        escalation.id,
        trimmed ? { action, text: trimmed } : { action },
      );
      // Deliberately not removed here: the entry stays visible until the next
      // run-change revision re-fetches the pending list and the backend no
      // longer returns it — the panel reflects what is actually on disk.
      setPhase("submitted");
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setPhase("conflict");
        onConflict(
          `Escalation ${escalation.id} was already answered elsewhere (CLI or another tab) — refreshing the list.`,
        );
      } else {
        setSubmitError(errorMessage(err));
        setPhase("idle");
      }
    }
  }

  return (
    <li className={`escalation-card${resolved ? " escalation-card--resolved" : ""}`}>
      <div className="escalation-card__meta">
        <span className="escalation-card__kind">
          {KIND_LABELS[escalation.kind] ?? escalation.kind}
        </span>
        <span className="escalation-card__group">{escalation.group_id}</span>
        <span className="escalation-card__generation">gen {escalation.generation}</span>
        <span className="escalation-card__created">{formatCreated(escalation.created_at)}</span>
      </div>

      {/* The full prompt, untruncated — a human is deciding whether an agent
        * should continue, so every word the broker curated is shown. */}
      <p className="escalation-card__prompt">{escalation.prompt}</p>

      {hasContext && (
        <dl className="escalation-card__context">
          {ctx.report_path && (
            <div className="escalation-card__context-row">
              <dt>report</dt>
              <dd className="escalation-card__path">{ctx.report_path}</dd>
            </div>
          )}
          {ctx.verdict_path && (
            <div className="escalation-card__context-row">
              <dt>verdict</dt>
              <dd className="escalation-card__path">{ctx.verdict_path}</dd>
            </div>
          )}
          {ctx.diff_summary && (
            <div className="escalation-card__context-row">
              <dt>diff</dt>
              <dd className="escalation-card__diff">{ctx.diff_summary}</dd>
            </div>
          )}
          {ctx.surprises.length > 0 && (
            <div className="escalation-card__context-row">
              <dt>surprises</dt>
              <dd>
                <ul className="escalation-card__surprises">
                  {ctx.surprises.map((surprise, index) => (
                    <li key={index}>
                      <span className="escalation-card__surprise-kind">[{surprise.kind}]</span>{" "}
                      {surprise.description}
                      {surprise.affected_groups.length > 0 && (
                        <span className="escalation-card__surprise-groups">
                          {" "}
                          (affects {surprise.affected_groups.join(", ")})
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              </dd>
            </div>
          )}
        </dl>
      )}

      <form className="escalation-card__form" onSubmit={submit}>
        {/* One fieldset disables every control while a request is in flight
          * (no double-submit) and after resolution (a retry would just 409). */}
        <fieldset className="escalation-card__fieldset" disabled={phase !== "idle"}>
          <div className="escalation-card__actions" role="radiogroup" aria-label="Action">
            {ACTIONS.map((value) => (
              <label key={value} className="escalation-card__action">
                <input
                  type="radio"
                  name={`escalation-action-${escalation.id}`}
                  value={value}
                  checked={action === value}
                  onChange={() => setAction(value)}
                />
                {value}
              </label>
            ))}
          </div>
          <textarea
            className="escalation-card__text"
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="Guidance for the agent (optional for skip / abort)"
            rows={3}
            aria-label="Response text"
          />
          <button type="submit" className="escalation-card__submit">
            {phase === "inflight" ? "Sending…" : `Send ${action}`}
          </button>
        </fieldset>
      </form>

      {phase === "submitted" && (
        <p className="escalation-card__status" role="status">
          Response recorded — this entry clears once the run confirms it.
        </p>
      )}
      {phase === "conflict" && (
        <p className="escalation-card__status escalation-card__status--conflict" role="status">
          Already answered elsewhere.
        </p>
      )}
      {submitError && <p className="escalation-card__error">{submitError}</p>}
    </li>
  );
}

// ------------------------------------------------------------------- panel

export interface EscalationPanelProps {
  project: string;
  runId: string;
  /** Advances on every run-directory change; refresh the pending list off it. */
  revision: number;
}

function EscalationPanel({ project, runId, revision }: EscalationPanelProps) {
  const [escalations, setEscalations] = useState<EscalationRequest[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Bumped on a 409 so the list refreshes immediately instead of waiting for
  // the next run-change event.
  const [refreshNonce, setRefreshNonce] = useState(0);

  // Reset on run switch so the previous run's escalations never linger.
  useEffect(() => {
    setEscalations(null);
    setListError(null);
    setNotice(null);
  }, [project, runId]);

  // Re-fetch off the run-change revision (and the conflict nonce) — never on
  // a fixed interval. This is also what clears resolved entries: the backend
  // stops listing them once the response file is on disk.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const next = await listEscalations(project, runId);
        if (cancelled) return;
        setEscalations(next);
        setListError(null);
      } catch (err) {
        if (cancelled) return;
        setListError(errorMessage(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [project, runId, revision, refreshNonce]);

  function handleConflict(message: string): void {
    setNotice(message);
    setRefreshNonce((current) => current + 1);
  }

  const pendingCount = escalations?.length ?? 0;

  return (
    <section className="escalation-panel" aria-label="Escalations">
      <div className="escalation-panel__header">
        <h2>Escalations</h2>
        {pendingCount > 0 && (
          <span className="escalation-panel__count" role="status">
            {pendingCount} pending
          </span>
        )}
      </div>

      {notice && (
        <p className="escalation-panel__notice" role="status">
          {notice}
        </p>
      )}
      {listError && <p className="escalation-panel__error">{listError}</p>}

      {escalations === null ? (
        !listError && <p className="escalation-panel__empty">Loading escalations…</p>
      ) : escalations.length === 0 ? (
        <p className="escalation-panel__empty">
          No pending escalations — the run is not waiting on an operator.
        </p>
      ) : (
        <ol className="escalation-panel__list">
          {escalations.map((escalation) => (
            <EscalationEntry
              key={escalation.id}
              project={project}
              runId={runId}
              escalation={escalation}
              onConflict={handleConflict}
            />
          ))}
        </ol>
      )}
    </section>
  );
}

export default EscalationPanel;
