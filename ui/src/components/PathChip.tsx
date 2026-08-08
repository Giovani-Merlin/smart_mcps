// An on-disk path, shown and copyable. An app-wide primitive, not a one-off.
//
// Every file-backed panel carries exactly one of these in its header, because
// the operator's next move after reading a panel is almost always to go open
// the file it came from — and a path they have to retype is a path they will
// not use. Middle-ellipsised rather than truncated: the run id and the filename
// are both at the ends, and those are the parts that identify it.
//
// Display and copy only. This deliberately does not fetch or serve the file:
// exposing a path is zero risk, and serving arbitrary paths is not, so the two
// stay separate concerns.

import { useCallback, useEffect, useState } from "react";
import "./PathChip.css";

export interface PathChipProps {
  path: string;
  /** What this path *is* — "trace", "manifest". Shown before the path. */
  label?: string;
  /** Characters to keep before the ellipsis grows. */
  max?: number;
}

const DEFAULT_MAX = 52;

export function PathChip({ path, label, max = DEFAULT_MAX }: PathChipProps) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1400);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const copy = useCallback(() => {
    void writeClipboard(path).then((ok) => setCopied(ok));
  }, [path]);

  return (
    <button
      type="button"
      className={`path-chip${copied ? " path-chip--copied" : ""}`}
      onClick={copy}
      // The full path, always, regardless of how the label was ellipsised.
      title={path}
      aria-label={`Copy path ${path}`}
    >
      {label && <span className="path-chip__label">{label}</span>}
      <code className="path-chip__path">{middleEllipsis(path, max)}</code>
      <span className="path-chip__hint">{copied ? "copied" : "copy"}</span>
    </button>
  );
}

/** Keeps both ends — the run id and the filename are what identify a path. */
export function middleEllipsis(path: string, max: number): string {
  if (path.length <= max) return path;
  const keep = max - 1;
  const head = Math.ceil(keep / 2);
  const tail = Math.floor(keep / 2);
  return `${path.slice(0, head)}…${path.slice(path.length - tail)}`;
}

/**
 * `navigator.clipboard` is unavailable over plain HTTP on anything but
 * localhost, and the Observatory is routinely opened at a LAN address. The
 * `execCommand` path is the fallback that still works there.
 */
async function writeClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall through — a denied permission is not a reason to give up.
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

export default PathChip;
