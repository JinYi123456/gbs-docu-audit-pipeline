"use client";

import { METHOD_LABELS, type FieldRow } from "@/lib/types";

const VERDICT_ROW: Record<FieldRow["verdict"], string> = {
  defect: "bg-defect-bg/60",
  undecided: "bg-undecided-bg/60",
  match: "",
};

const VERDICT_BADGE: Record<FieldRow["verdict"], string> = {
  defect: "bg-defect-text text-white",
  undecided: "bg-undecided-text text-white",
  match: "bg-ok-text text-white",
};

const VERDICT_LABEL: Record<FieldRow["verdict"], string> = {
  defect: "MISMATCHED",
  undecided: "UNDECIDED",
  match: "MATCHED",
};

function Cell({
  value,
  normalized,
  highlight,
}: {
  value: string | null;
  normalized: string | null;
  highlight: boolean;
}) {
  const empty = value === null || value.trim() === "";
  return (
    <div
      className={`mono-cell ${highlight ? "font-semibold" : ""} ${
        empty ? "text-slate-400 italic" : "text-slate-800"
      }`}
    >
      {empty ? "(blank)" : value}
      {normalized && normalized !== value && !empty ? (
        <div className="mt-0.5 text-[10px] text-slate-400">normalised: {normalized}</div>
      ) : null}
    </div>
  );
}

export default function FieldDiff({
  fields,
  selected,
  onToggle,
}: {
  fields: FieldRow[];
  selected: string[];
  onToggle?: (field: string) => void;
}) {
  if (fields.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-slate-300 bg-slate-50/60 p-4 text-xs text-slate-500">
        No comparable SI / draft-BL fields on this email — no attachment, or the attachment is not a
        counterpart document. Routed to human escalation instead of guessing.
      </p>
    );
  }

  return (
    <div className="overflow-hidden rounded-lg border border-slate-200">
      {/*
        Verdict column must never squeeze into a vertical stack of single characters.
        Guard: fixed column widths + whitespace-nowrap on the verdict cell + a min table
        width with horizontal scroll, so narrow windows scroll instead of crushing the column.
      */}
      <div className="scroll-thin overflow-x-auto">
        <table className="w-full min-w-[880px] table-fixed border-collapse text-left">
          <thead className="bg-slate-50 text-[10px] uppercase tracking-[0.12em] text-slate-500">
            <tr>
              <th className="w-[22%] px-3 py-2 font-semibold">Field</th>
              <th className="w-[26%] px-3 py-2 font-semibold">Shipping Instruction</th>
              <th className="w-[26%] px-3 py-2 font-semibold">Draft Bill of Lading</th>
              <th className="w-[26%] px-3 py-2 font-semibold">Verdict</th>
            </tr>
          </thead>
          <tbody>
            {fields.map((row) => {
              const isSelected = selected.includes(row.field);
              return (
                <tr
                  key={row.field}
                  className={`border-t border-slate-200 align-top ${VERDICT_ROW[row.verdict]} ${
                    row.verdict === "defect" ? "ring-2 ring-inset ring-defect-border" : ""
                  }`}
                >
                  <td className="px-3 py-2.5">
                    <label className="flex cursor-pointer items-start gap-2">
                      {onToggle ? (
                        <input
                          type="checkbox"
                          checked={isSelected}
                          onChange={() => onToggle(row.field)}
                          className="mt-0.5 h-3.5 w-3.5 accent-slate-900"
                        />
                      ) : null}
                      <span>
                        <span className="block text-[12.5px] font-semibold text-slate-800">
                          {row.label}
                        </span>
                        <span className="mono-cell text-slate-400">{row.field}</span>
                      </span>
                    </label>
                  </td>
                  <td className="px-3 py-2.5">
                    <Cell
                      value={row.si_raw}
                      normalized={row.si_normalized}
                      highlight={row.verdict === "defect"}
                    />
                  </td>
                  <td className="px-3 py-2.5">
                    <Cell
                      value={row.bl_raw}
                      normalized={row.bl_normalized}
                      highlight={row.verdict === "defect"}
                    />
                  </td>
                  <td className="px-3 py-2.5">
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 whitespace-nowrap">
                      <span
                        className={`pill border-transparent ${VERDICT_BADGE[row.verdict]}`}
                        title={row.rationale ?? undefined}
                      >
                        {VERDICT_LABEL[row.verdict]}
                      </span>
                      <span className="rounded border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] font-medium text-slate-500">
                        {METHOD_LABELS[row.match_method] ?? row.match_method}
                      </span>
                    </div>
                    <div className="mono-cell mt-1 whitespace-nowrap text-slate-400">
                      {row.delta !== null
                        ? `Δ ${row.delta}`
                        : row.similarity !== null
                          ? `sim ${row.similarity.toFixed(3)}`
                          : null}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {fields.some((row) => row.verdict !== "match" && row.rationale) ? (
        <ul className="space-y-1 border-t border-slate-200 bg-white px-3 py-2 text-[11px] leading-4 text-slate-500">
          {fields
            .filter((row) => row.verdict !== "match" && row.rationale)
            .map((row) => (
              <li key={`why-${row.field}`}>
                <span className="font-semibold text-slate-600">{row.label}:</span> {row.rationale}
              </li>
            ))}
        </ul>
      ) : null}
    </div>
  );
}
