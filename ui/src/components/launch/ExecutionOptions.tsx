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

import { useState } from "react";

import type { ExecutionOptions as Options, ResolvedOptions } from "../../types";
import { ESCALATION_INTENSITIES } from "../../types";
import "./ExecutionOptions.css";

export interface ExecutionOptionsProps {
  value: Options;
  onChange: (next: Options) => void;
  /** Distinguishes the two instances' input ids so labels stay bound to their
   * own controls when both forms are on the page. */
  idPrefix: string;
  disabled?: boolean;
  /** What every unspecified field on this form would actually resolve to
   * (plan U18/F14) — absent while the fetch is still in flight, in which case
   * the fields fall back to the same generic "(from config)" text they always
   * had. */
  resolved?: ResolvedOptions | null;
}

/** "(from config)" with the real value appended once it is known, so a field
 * left unspecified no longer hides what that actually means. */
function fromConfig(resolved: string | number | undefined): string {
  return resolved === undefined ? "(from config)" : `(from config: ${resolved})`;
}

const PERMISSION_MODES = ["acceptEdits", "plan", "default", "bypassPermissions"];
const REVIEW_INTENSITIES = ["self_verify", "paired", "paired_plus"];
const SOURCES = ["workers_via_orchestrator", "orchestrator_only"];

/** "" in a select means unset — the CLI flag is omitted and the config decides. */
function pick(raw: string): string | null {
  return raw === "" ? null : raw;
}

/** Sentinel option value for "type a model id yourself" — never a real model. */
const CUSTOM_MODEL = "__custom__";

/**
 * One model knob: a `<select>` over the known model list (F2) with a free-text
 * escape hatch. The dropdown exists because these were free-text `<input>`s and
 * a typo silently became a bad model — `claude -p --model <bogus>` exits 0, so
 * nothing downstream would ever complain. The escape hatch stays because the
 * list is hard-coded (there is no trustworthy discovery), so a newer model id
 * must always remain enterable.
 */
export function ModelField({
  id,
  label,
  value,
  models,
  placeholder,
  onChange,
}: {
  id: string;
  label: string;
  value: string | null;
  models: string[];
  placeholder: string;
  onChange: (next: string | null) => void;
}) {
  // Custom mode is sticky while the operator types: `value` alone cannot carry
  // it, because a half-typed id that happens to be "" or match a list entry
  // would snap the control back to the dropdown mid-keystroke.
  const [customMode, setCustomMode] = useState(false);
  const custom = customMode || (value !== null && !models.includes(value));
  return (
    <label htmlFor={id}>
      {label}
      <select
        id={id}
        value={custom ? CUSTOM_MODEL : (value ?? "")}
        onChange={(e) => {
          if (e.target.value === CUSTOM_MODEL) {
            setCustomMode(true);
            return;
          }
          setCustomMode(false);
          onChange(pick(e.target.value));
        }}
      >
        <option value="">{placeholder}</option>
        {models.map((model) => (
          <option key={model} value={model}>
            {model}
          </option>
        ))}
        <option value={CUSTOM_MODEL}>other…</option>
      </select>
      {custom && (
        <input
          id={`${id}-custom`}
          type="text"
          value={value ?? ""}
          placeholder="model id"
          aria-label={`${label} (custom)`}
          onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
        />
      )}
    </label>
  );
}

export function ExecutionOptionsForm({
  value,
  onChange,
  idPrefix,
  disabled = false,
  resolved,
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
            <option value="">{fromConfig(resolved?.escalation_intensity)}</option>
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
            <option value="">{fromConfig(resolved?.escalation_source)}</option>
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
            <option value="">{fromConfig(resolved?.permission_mode)}</option>
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
            // A blank number input reads as "no idea what this does" — the
            // library default of 1 ran a thirteen-group, three-wide DAG
            // serially with nothing on the form suggesting that. The
            // placeholder names the actual resolved value instead (F14).
            placeholder={String(resolved?.concurrency ?? 1)}
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
            placeholder={
              resolved?.escalation_timeout != null
                ? String(resolved.escalation_timeout)
                : "block forever"
            }
            onChange={(e) =>
              set({ escalation_timeout: e.target.value === "" ? null : Number(e.target.value) })
            }
          />
        </label>

        <ModelField
          id={id("model-worker")}
          label="Worker model"
          value={value.model_worker ?? null}
          models={resolved?.known_models ?? []}
          placeholder={fromConfig(resolved?.model_worker)}
          onChange={(model_worker) => set({ model_worker })}
        />

        <ModelField
          id={id("model-base")}
          label="Orchestrator (base) model"
          value={value.model_base ?? null}
          models={resolved?.known_models ?? []}
          placeholder={fromConfig(resolved?.model_base)}
          onChange={(model_base) => set({ model_base })}
        />

        <ModelField
          id={id("model-speccer")}
          label="Rewrite speccer model (mid-run spec rewrites)"
          value={value.model_speccer ?? null}
          models={resolved?.known_models ?? []}
          placeholder={fromConfig(resolved?.model_speccer)}
          onChange={(model_speccer) => set({ model_speccer })}
        />
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
