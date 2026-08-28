// What a selected grouping actually contains, read on the launch page instead
// of by a throwaway `group --dry-run` in a terminal.
//
// Read-only by construction: this component takes no `onChange`, posts
// nothing, and renders exactly the fields `_print_report` prints per group —
// name, tasks, files, token estimate, difficulty/intensity and dependencies —
// sourced from the same `groups.json` the CLI reads, so the two views cannot
// drift apart.

import { useEffect, useState } from "react";

import { errorMessage, getGroupingPreview } from "../../api";
import type { GroupingPreview as GroupingPreviewData } from "../../types";
import "./GroupingPreview.css";

export interface GroupingPreviewProps {
  project: string;
  name: string;
}

export function GroupingPreview({ project, name }: GroupingPreviewProps) {
  const [preview, setPreview] = useState<GroupingPreviewData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setPreview(null);
    setError(null);
    if (!name) return;
    void (async () => {
      try {
        const next = await getGroupingPreview(project, name);
        if (!cancelled) setPreview(next);
      } catch (err) {
        if (!cancelled) setError(errorMessage(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [project, name]);

  if (!name) return null;

  if (error) {
    return (
      <section className="grouping-preview grouping-preview--error" aria-label="Grouping preview">
        <p className="grouping-preview__empty">{error}</p>
      </section>
    );
  }

  if (!preview) {
    return (
      <section className="grouping-preview" aria-label="Grouping preview">
        <p className="grouping-preview__empty">Loading…</p>
      </section>
    );
  }

  if (!preview.present) {
    return (
      <section className="grouping-preview" aria-label="Grouping preview">
        <p className="grouping-preview__empty">
          {preview.missing ?? "this grouping has no groups.json yet"}
        </p>
      </section>
    );
  }

  return (
    <section className="grouping-preview" aria-label="Grouping preview">
      <p className="grouping-preview__summary">
        {preview.groups.length} group(s) from {preview.plan_path}
      </p>
      {preview.flags.length > 0 && (
        <ul className="grouping-preview__flags">
          {preview.flags.map((flag) => (
            <li key={flag}>{flag}</li>
          ))}
        </ul>
      )}
      <ul className="grouping-preview__groups">
        {preview.groups.map((group) => (
          <li key={group.id} className="grouping-preview__group">
            <h4>
              {group.id}: {group.name}
            </h4>
            <p className="grouping-preview__group-summary">{group.summary}</p>
            <dl>
              <dt>tasks</dt>
              <dd>{group.tasks.length > 0 ? group.tasks.join(", ") : "none"}</dd>
              <dt>files</dt>
              <dd>{group.files.length > 0 ? group.files.join(", ") : "none"}</dd>
              <dt>est. tokens</dt>
              <dd>{group.estimated_tokens}</dd>
              <dt>difficulty</dt>
              <dd>
                {group.difficulty.toFixed(2)} → {group.intensity}
              </dd>
              <dt>depends on</dt>
              <dd>{group.dependencies.length > 0 ? group.dependencies.join(", ") : "none"}</dd>
              <dt>verification</dt>
              <dd>{group.verification_count} item(s)</dd>
            </dl>
          </li>
        ))}
      </ul>
    </section>
  );
}

export default GroupingPreview;
