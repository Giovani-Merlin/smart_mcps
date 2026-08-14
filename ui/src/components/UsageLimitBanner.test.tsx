import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import UsageLimitBanner, { formatCountdown } from "./UsageLimitBanner";

afterEach(cleanup);

const armed = {
  armed_at: "2026-08-13T10:00:00Z",
  detail: "You've hit your session limit · resets 1pm (Europe/Berlin)",
  attempt: 1,
  reset_at: "2999-01-01T12:00:00Z",
  wake_at: "2999-01-01T12:01:00Z",
  released_at: null,
};

describe("the usage-limit banner", () => {
  it("renders nothing when the run has never hit a limit", () => {
    const { container } = render(<UsageLimitBanner usageLimit={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("clears itself once the pause is released", () => {
    const { container } = render(
      <UsageLimitBanner usageLimit={{ ...armed, released_at: "2026-08-13T11:00:00Z" }} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("shows the reset time and the provider's own words, verbatim", () => {
    render(<UsageLimitBanner usageLimit={armed} />);
    expect(screen.getByText(/Paused — usage limit/)).toBeTruthy();
    // Verbatim, because the wordings are undocumented and differ by limit type:
    // paraphrasing destroys the only evidence of which limit was hit.
    expect(screen.getByText(armed.detail)).toBeTruthy();
  });

  it("says it is polling when the limit gave no reset time", () => {
    render(<UsageLimitBanner usageLimit={{ ...armed, reset_at: null, wake_at: null }} />);
    expect(screen.getByText(/no reset time given/)).toBeTruthy();
  });

  it("never counts down past zero", () => {
    // A reset that passed while the retry is in flight is ordinary, and a
    // negative countdown would read as a bug in the run rather than a moment.
    expect(formatCountdown(-5000)).toBe("any moment now");
    expect(formatCountdown(0)).toBe("any moment now");
    expect(formatCountdown(45_000)).toBe("45s");
    expect(formatCountdown(3 * 60 * 1000 + 5000)).toBe("3m 05s");
    expect(formatCountdown(2 * 3600 * 1000 + 7 * 60 * 1000)).toBe("2h 07m");
  });
});
