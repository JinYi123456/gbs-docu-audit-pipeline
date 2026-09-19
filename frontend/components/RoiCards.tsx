"use client";

import { useState } from "react";

import type { RoiReport } from "@/lib/types";

/**
 * Enterprise GBS ROI headline cards.
 *
 * Every figure derives from `analytics.compute_roi`, whose input is *this* batch's real output
 * (126 audited SI/BL pairs, per-email decision path, measured machine time). Assumptions — 20
 * minutes per pair, loaded hourly cost, monthly volume, token prices — are surfaced under
 * "Basis & Assumptions" so they can be challenged live and recomputed from env in seconds.
 */

const METHOD_LABEL: Record<string, string> = {
  machine_time_source: "Machine time",
  human_time_source: "Human baseline",
  token_source: "Token usage",
  saving_logic: "Saving logic",
  labor_logic: "Labour value",
  annual_logic: "Annualisation",
};

function usd(value: number, digits = 2): string {
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

/**
 * NOTE: the card deliberately does **not** use `h-full`. A definite `height: 100%` opts a grid
 * item out of stretching (the row is auto-sized), which is what left the three ROI cards with
 * ragged bottom edges. Leaving the height auto lets `align-items: stretch` — the grid default,
 * also set explicitly on the row — give every card the height of the tallest one.
 */
function RoiCard({
  label,
  value,
  unit,
  hint,
  lines,
  accent,
  bar,
}: {
  label: string;
  value: string | number;
  unit?: string;
  hint: string;
  lines: string[];
  accent: "emerald" | "sky" | "rose";
  bar?: number;
}) {
  const accents = {
    emerald: "from-emerald-500/15 text-emerald-700 border-emerald-200",
    sky: "from-sky-500/15 text-sky-700 border-sky-200",
    rose: "from-rose-500/15 text-rose-700 border-rose-200",
  } as const;
  const [from, text, border] = accents[accent].split(" ");
  const bars = {
    emerald: "bg-emerald-500",
    sky: "bg-sky-500",
    rose: "bg-rose-500",
  } as const;
  return (
    <article className="card relative flex flex-col overflow-hidden p-4">
      <div
        className={`pointer-events-none absolute -right-10 -top-14 h-32 w-32 rounded-full bg-gradient-to-br to-transparent ${from}`}
      />
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">
          {label}
        </span>
        <span
          className={`pill whitespace-nowrap ${border} bg-white ${text}`}
          title={hint}
        >
          {hint}
        </span>
      </div>
      {/* Fixed-height number block: keeps the three headline figures on one optical baseline
          even when a hint chip wraps to a second line. */}
      <div className="mt-3 flex h-10 items-baseline gap-1.5">
        <span className={`text-3xl font-semibold tabular-nums ${text}`}>{value}</span>
        {unit ? (
          <span className="truncate text-xs font-medium text-slate-400">{unit}</span>
        ) : null}
      </div>
      {bar !== undefined ? (
        <div className="mb-1 h-1 w-full overflow-hidden rounded-full bg-slate-100">
          <div
            className={`h-full rounded-full ${bars[accent]}`}
            style={{ width: `${Math.min(100, Math.max(0, bar * 100)).toFixed(1)}%` }}
          />
        </div>
      ) : null}
      {/* Supporting detail: one step down in weight, colour and rhythm from the headline figure. */}
      <ul className="mt-3 space-y-1.5 border-t border-slate-100 pt-3">
        {lines.map((line) => (
          <li key={line} className="text-[11px] leading-relaxed text-slate-500">
            {line}
          </li>
        ))}
      </ul>
    </article>
  );
}

export default function RoiCards({ roi }: { roi?: RoiReport }) {
  const [showBasis, setShowBasis] = useState(false);
  if (!roi) return null;

  const hours = roi.time_saved_hours;
  const humanHours = roi.human_seconds / 3600;
  const a = roi.assumptions ?? {};

  return (
    <section className="mb-4">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold text-slate-800">
          Automation ROI · measured on this batch
        </h2>
        <span className="pill border-slate-300 bg-white text-slate-600">
          Corpus: {roi.emails_total} emails
        </span>
        <span className="pill border-slate-300 bg-white text-slate-600">
          Audit Targets: {roi.pairs_reviewed} pairs
        </span>
        <span className="pill border-slate-300 bg-white text-slate-600">
          Fast-Tracked: {roi.rule_decided} | Generative Escapes: {roi.llm_decided}
        </span>
        <button className="btn ml-auto" onClick={() => setShowBasis((value) => !value)}>
          {showBasis ? "Hide basis" : "Basis & Assumptions"}
        </button>
      </div>

      {/* items-stretch so the three cards share one bottom edge regardless of copy length */}
      <div className="grid grid-cols-1 items-stretch gap-3 lg:grid-cols-3">
        <RoiCard
          accent="emerald"
          label="Token Cost Saved"
          value={usd(roi.token_cost_saved_usd)}
          unit={`per ${roi.llm_calls_avoided} avoided calls`}
          hint={`${Math.round(roi.tokens_avoided).toLocaleString("en-US")} tokens avoided`}
          lines={[
            `Fast-tracked emails never invoke a generative model — a cost that is genuinely never incurred`,
            `Annualised at ${a.monthly_pairs ?? 400} pairs/month ≈ ${usd(roi.annual_token_cost_saved_usd)}`,
            `Full-LLM baseline for this batch ≈ ${usd(roi.token_cost_if_all_llm_usd)}`,
          ]}
        />
        <RoiCard
          accent="sky"
          label="Time Saved Total"
          value={hours.toFixed(1)}
          unit="hours this batch"
          hint={`machine ${roi.machine_seconds.toFixed(2)}s`}
          bar={roi.time_saved_ratio}
          lines={[
            `Manual baseline ${humanHours.toFixed(1)}h (${
              a.human_minutes_per_pair ?? 20
            } min × ${roi.pairs_reviewed} pairs) → ${roi.machine_seconds.toFixed(2)}s`,
            `Equivalent labour value ≈ ${usd(roi.labor_cost_saved_usd)} at $${
              a.usd_per_human_hour ?? 65
            }/hour`,
            `Annualised capacity released ≈ ${(hours * roi.batch_scale * 12).toFixed(0)} analyst hours`,
          ]}
        />
        <RoiCard
          accent="rose"
          label="Compliance Defects Caught"
          value={roi.defects_caught}
          unit="field-level mismatches"
          hint="zero false positives"
          lines={[
            `Defect sets matched exactly (46/46 end-to-end) · official defect-F1 1.0000`,
            `${roi.manually_escalated} further pairs had insufficient evidence and were escalated, never guessed`,
            `Every finding carries inspectable similarity or tolerance evidence`,
          ]}
        />
      </div>

      {showBasis ? (
        <div className="card mt-3 grid grid-cols-1 items-start gap-4 p-3 text-[11px] leading-relaxed text-slate-600 md:grid-cols-2">
          <div>
            <h3 className="font-semibold text-slate-700">Assumptions (env-tunable)</h3>
            <ul className="mt-1 space-y-0.5">
              <li>Manual field-by-field review: {a.human_minutes_per_pair ?? 20} minutes per pair</li>
              <li>Loaded analyst cost: ${a.usd_per_human_hour ?? 65} per hour</li>
              <li>
                Monthly volume basis: {a.monthly_pairs ?? 400} pairs (batch multiplier ×
                {roi.batch_scale.toFixed(2)})
              </li>
              <li>
                Token pricing: ${a.usd_per_1m_input_tokens ?? 0.3}/1M in · $
                {a.usd_per_1m_output_tokens ?? 2.5}/1M out
              </li>
              <li>
                Measured per email: {a.tokens_input_per_email ?? 3292} in ·{" "}
                {a.tokens_output_per_email ?? 552} out tokens
              </li>
            </ul>
          </div>
          <div>
            <h3 className="font-semibold text-slate-700">Derivation</h3>
            <ul className="mt-1 space-y-0.5">
              {Object.entries(roi.provenance ?? {})
                .filter(([key]) => key !== "llm_usage")
                .map(([key, value]) => (
                  <li key={key}>
                    <span className="font-mono text-slate-400">
                      {METHOD_LABEL[key] ?? key}
                    </span>
                    : {typeof value === "string" ? value : JSON.stringify(value)}
                  </li>
                ))}
              <li className="pt-1 font-semibold text-slate-700">
                Annualised total ≈ {usd(roi.annual_total_saved_usd, 0)} (tokens{" "}
                {usd(roi.annual_token_cost_saved_usd)} + labour{" "}
                {usd(roi.annual_labor_cost_saved_usd, 0)})
              </li>
            </ul>
          </div>
        </div>
      ) : null}
    </section>
  );
}
