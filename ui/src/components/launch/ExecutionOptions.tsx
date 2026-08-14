// One control block for every execution option, shared by the run and resume
// forms.
//
// Shared rather than duplicated for a specific reason. `--intensity` is
// droppable on a terminal `resume`, and omitting it silently reverts a run to
// block-forever HITL — a trap that cost a whole session. Two hand-written forms
// would reproduce that trap the first time one of them grew a field the other
// did not, so resume and run render the *same* control block and a tier chosen
// here can never be "the one the other form forgot".
//
// Every control is three-state where the CLI flag is: unset means "not
// specified" and lets the run's config file decide, exactly as an omitted flag
// does. That is why the selects carry an explicit "(from config)" option rather
// than defaulting to a value this form invented.

import type { ExecutionOptions as Options } from "../../types";
import { ESCALATION_INTENSITIES } from "../../types";
import "./ExecutionOptions.css";

export interface ExecutionOptionsProps {
  value: Options;
  onChange: (next: Options) => void;
  /** Distinguishes the two instances' input ids so labels stay bound to their
   * own controls when both forms are on the page. */
  idPrefix: string;
  disabled?: boolean;
}

const PERMISSION_MODES = ["acceptEdits", "plan", "default", "bypassPermissions"];
const REVIEW_INTENSITIES = ["self_verify", "paired", "paired_plus"];
const SOURCES = ["workers_via_orchestrator", "orchestrator_only"];

/** "" in a select means unset — the CLI flag is omitted and the config decides. */
function pick(raw: string): string | null {
  return raw === "" ? null : raw;
}

export function ExecutionOptionsForm({
  value,
  onChange,
  idPrefix,
  disabled = false,
}: ExecutionOptionsProps) {
  const set = (patch: Partial<Options>) => onChange({ ...value, ...patch });
  const id = (name: string) => `${idPrefix}-${name}`;

  return (
    <fieldset className="exec-options" disabled={disabled}>
      <legend>Execution options</legend>

      <div className="exec-options__grid">
        <label htmlFor={id("intensity")}>
          Escalation tier
          <select
            id={id("intensity")}
            value={value.intensity ?? ""}
            onChange={(e) =>
              set({ intensity: (pick(e.target.value) as Options["intensity"]) ?? null })
            }
          >
            <option value="">(from config)</option>
            {ESCALATION_INTENSITIES.map((tier) => (
              <option key={tier} value={tier}>
                {tier}
              </option>
            ))}
          </select>
        </label>

        <label htmlFor={id("source")}>
          Escalation source
          <select
            id={id("source")}
            value={value.escalation_source ?? ""}
            onChange={(e) =>
              set({
                escalation_source: (pick(
                  e.target.value,
                ) as Options["escalation_source"]) ?? null,
              })
            }
          >
            <option value="">(from config)</option>
            {SOURCES.map((source) => (
              <option key={source} value={source}>
                {source}
              </option>
            ))}
          </select>
        </label>

        <label htmlFor={id("permission")}>
          Permission mode
          <select
            id={id("permission")}
            value={value.permission_mode ?? ""}
            onChange={(e) => set({ permission_mode: pick(e.target.value) })}
          >
            <option value="">(from config)</option>
            {PERMISSION_MODES.map((mode) => (
              <option key={mode} value={mode}>
                {mode}
              </option>
            ))}
          </select>
        </label>

        <label htmlFor={id("review")}>
          Review intensity
          <select
            id={id("review")}
            value={value.review_intensity ?? ""}
            onChange={(e) => set({ review_intensity: pick(e.target.value) })}
          >
            <option value="">(each group's own)</option>
            {REVIEW_INTENSITIES.map((intensity) => (
              <option key={intensity} value={intensity}>
                {intensity}
              </option>
            ))}
          </select>
        </label>

        <label htmlFor={id("concurrency")}>
          Concurrency
          <input
            id={id("concurrency")}
            type="number"
            min={1}
            value={value.concurrency ?? ""}
            placeholder="from config"
            onChange={(e) =>
              set({ concurrency: e.target.value === "" ? null : Number(e.target.value) })
            }
          />
        </label>

        <label htmlFor={id("timeout")}>
          Escalation timeout (s)
          <input
            id={id("timeout")}
            type="number"
            min={0}
            value={value.escalation_timeout ?? ""}
            placeholder="block forever"
            onChange={(e) =>
              set({ escalation_timeout: e.target.value === "" ? null : Number(e.target.value) })
            }
          />
        </label>
      </div>

      <div className="exec-options__checks">
        <label htmlFor={id("hitl")}>
          <input
            id={id("hitl")}
            type="checkbox"
            checked={Boolean(value.hitl)}
            onChange={(e) => set({ hitl: e.target.checked })}
          />
          Human in the loop
        </label>

        <label htmlFor={id("sequential")}>
          <input
            id={id("sequential")}
            type="checkbox"
            checked={Boolean(value.sequential)}
            onChange={(e) => set({ sequential: e.target.checked })}
          />
          Sequential (one group at a time)
        </label>

        <label htmlFor={id("auto-resume")}>
          <input
            id={id("auto-resume")}
            type="checkbox"
            // Three-state collapsed to two here on purpose: this is the one
            // option where "unset" and "on" behave identically (auto-resume is
            // on by default), so a tri-state control would be a distinction
            // without a difference. Unticking sends `--no-auto-resume`.
            checked={value.auto_resume !== false}
            onChange={(e) => set({ auto_resume: e.target.checked ? null : false })}
          />
          Wait out usage limits
        </label>
      </div>
    </fieldset>
  );
}

export default ExecutionOptionsForm;
