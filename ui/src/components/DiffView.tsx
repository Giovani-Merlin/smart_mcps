// Renders a `DiffResult` (plan U29) — a group's whole diff against the
// integration tip it branched from, or a generation's final diff. Both come
// off the same backend contract: `available: false` covers every degrade
// path (a torn-down branch, missing manifest data, no recorded session
// timing) with a human `reason`, so this component never has to distinguish
// "no diff" from "an error fetching it" — the caller passes `error` only for
// the request itself failing (network, 5xx).

import type { DiffResult } from "../types";
import "./DiffView.css";

interface DiffFile {
  /** The `diff --git a/x b/x` header line, or "" for content before the
   * first header (nothing this backend emits, but never silently dropped). */
  header: string;
  path: string;
  lines: string[];
}

/** Split a unified diff into per-file sections on `diff --git` headers, so
 * each file gets its own header the way `git diff` itself groups them. */
function splitDiffIntoFiles(diff: string): DiffFile[] {
  if (!diff) return [];
  const files: DiffFile[] = [];
  let current: DiffFile | null = null;
  for (const line of diff.split("\n")) {
    const match = /^diff --git a\/(.+) b\/(.+)$/.exec(line);
    if (match) {
      if (current) files.push(current);
      current = { header: line, path: match[2], lines: [] };
      continue;
    }
    if (current === null) {
      current = { header: "", path: "", lines: [] };
    }
    current.lines.push(line);
  }
  if (current) files.push(current);
  return files;
}

function lineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) {
    return "diff-view__line diff-view__line--meta";
  }
  if (line.startsWith("@@")) return "diff-view__line diff-view__line--hunk";
  if (line.startsWith("+")) return "diff-view__line diff-view__line--add";
  if (line.startsWith("-")) return "diff-view__line diff-view__line--del";
  return "diff-view__line";
}

export interface DiffViewProps {
  /** Omit when a heading is already rendered by the caller (the generation
   * diff sits under its own header with a generation picker). */
  title?: string;
  /** `null` means "still loading". */
  result: DiffResult | null;
  /** The request itself failed — distinct from `result.available === false`,
   * which is the backend's own honest "nothing to show" answer. */
  error?: string | null;
}

function DiffView({ title, result, error }: DiffViewProps) {
  const files = result?.available ? splitDiffIntoFiles(result.diff) : [];
  return (
    <div className="diff-view">
      {title && <h4>{title}</h4>}
      {error ? (
        <p className="drill-in__error">{error}</p>
      ) : result === null ? (
        <p className="drill-in__empty">Loading diff…</p>
      ) : !result.available ? (
        <p className="drill-in__empty">{result.reason ?? "diff unavailable"}</p>
      ) : result.diff.trim() === "" ? (
        <p className="drill-in__empty">No changes.</p>
      ) : (
        <>
          {result.truncated && (
            <p className="diff-view__truncated">
              Truncated to {result.diff.length.toLocaleString()} of{" "}
              {result.total_bytes?.toLocaleString() ?? "?"} characters — showing the first part
              only.
            </p>
          )}
          <div className="diff-view__scroll">
            {files.map((file, index) => (
              <div className="diff-view__file" key={index}>
                {file.path && <div className="diff-view__file-header">{file.path}</div>}
                <pre className="diff-view__body">
                  {file.lines.map((line, lineIndex) => (
                    <span className={lineClass(line)} key={lineIndex}>
                      {line}
                      {"\n"}
                    </span>
                  ))}
                </pre>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export default DiffView;
