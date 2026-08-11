// Every on-disk path this run is backed by, in one place, per route.
//
// `PathChip` puts one path in the header of the panel it belongs to. This is
// the other half: the operator who is about to leave the browser and go read
// the run directory wants all of them at once, so this lists every entry the
// backend reports and offers a single copy-all.
//
// Like the chip, display and copy only — it never fetches file contents. The
// backend does have a `/file` endpoint; wiring it in here would turn a
// zero-risk path listing into a file server, and those stay separate concerns.
//
// A missing artifact is listed, not hidden. The path an artifact *would* have
// had is the one the operator most wants when a panel is empty.

import { useEffect, useState } from "react";

import { errorMessage, getRunPaths } from "../api";
import type { PathEntry } from "../types";
import PathChip from "./PathChip";

export interface PathsDrawerProps {
  project: string;
  runId: string;
  /** Shows only the entries a route cares about; omitted means all of them. */
  panel?: string;
}

export function PathsDrawer({ project, runId, panel }: PathsDrawerProps) {
  const [entries, setEntries] = useState<PathEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setEntries(null);
    setError(null);
    void (async () => {
      try {
        const view = await getRunPaths(project, runId);
        if (!cancelled) setEntries(view.entries);
      } catch (err) {
        if (!cancelled) setError(errorMessage(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [project, runId]);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1400);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const shown = (entries ?? []).filter((entry) => !panel || entry.panel === panel);

  async function copyAll(): Promise<void> {
    // One path per line: what pastes usefully into a terminal or an editor's
    // file picker, which is where these are going.
    const text = shown.map((entry) => entry.path).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <details className="paths-drawer">
      <summary className="paths-drawer__summary">paths on disk</summary>
      <div className="paths-drawer__body">
        {error && <p className="paths-drawer__error">{error}</p>}
        {entries === null && !error && <p className="paths-drawer__missing">Loading paths…</p>}
        {entries !== null && shown.length === 0 && (
          <p className="paths-drawer__missing">No paths reported for this run.</p>
        )}
        {shown.length > 0 && (
          <>
            <div className="paths-drawer__actions">
              <button
                type="button"
                className="paths-drawer__copy-all"
                onClick={() => void copyAll()}
              >
                copy all
              </button>
              {copied && (
                <span className="paths-drawer__status" role="status">
                  {shown.length} paths copied
                </span>
              )}
            </div>
            <ul className="paths-drawer__entries">
              {shown.map((entry) => (
                <li key={entry.key}>
                  <PathChip path={entry.path} label={entry.label} />
                  {!entry.exists && <span className="paths-drawer__missing">does not exist</span>}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </details>
  );
}

export default PathsDrawer;
