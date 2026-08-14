// "This run is paused, not wedged."
//
// A run that has stopped because the account is out of budget looks, from every
// other surface, exactly like a run that has hung: the board stops moving and
// nothing says why. This banner is the answer, and it says three things — when
// the limit releases, how long that is from now, and the provider's own words,
// verbatim. The verbatim text matters because the wordings are undocumented and
// differ by limit type; paraphrasing it would destroy the only evidence an
// operator has about which limit they hit.
//
// It renders nothing at all once `released_at` is set, so it clears itself
// without needing a second signal.

import { useEffect, useState } from "react";

import type { UsageLimitView } from "../types";
import "./UsageLimitBanner.css";

export interface UsageLimitBannerProps {
  usageLimit?: UsageLimitView | null;
}

/** `h m s` down to zero, then "any moment now" — never a negative countdown,
 * which is what a reset that has passed while the retry is in flight produces. */
export function formatCountdown(msRemaining: number): string {
  if (msRemaining <= 0) return "any moment now";
  const total = Math.floor(msRemaining / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return `${seconds}s`;
}

export function UsageLimitBanner({ usageLimit }: UsageLimitBannerProps) {
  const paused = Boolean(usageLimit && !usageLimit.released_at);
  const resetAt = usageLimit?.reset_at ?? usageLimit?.wake_at ?? null;
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!paused || !resetAt) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [paused, resetAt]);

  if (!usageLimit || !paused) return null;

  const target = resetAt ? Date.parse(resetAt) : NaN;
  const countdown = Number.isNaN(target) ? null : formatCountdown(target - now);

  return (
    <div className="usage-limit" role="status">
      <div className="usage-limit__head">
        <strong>Paused — usage limit</strong>
        {resetAt && !Number.isNaN(target) ? (
          <span className="usage-limit__when">
            resumes {new Date(target).toLocaleString()} · {countdown}
          </span>
        ) : (
          // No parseable reset time: the gate polls rather than guessing, and
          // saying so is better than showing a countdown we invented.
          <span className="usage-limit__when">no reset time given — re-checking periodically</span>
        )}
      </div>
      <p className="usage-limit__detail">{usageLimit.detail}</p>
      <p className="usage-limit__note">
        The run is waiting in place and will retry the same call — nothing has been lost and no
        generation was spent. Attempt {usageLimit.attempt}.
      </p>
    </div>
  );
}

export default UsageLimitBanner;
