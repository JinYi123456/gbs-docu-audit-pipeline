"use client";

import { useMemo, useState } from "react";

import AgentTrace from "@/components/AgentTrace";
import FieldDiff from "@/components/FieldDiff";
import LiveStream from "@/components/LiveStream";
import RoiCards from "@/components/RoiCards";
import UploadPanel from "@/components/UploadPanel";
import {
  CATEGORY_STYLES,
  REASON_FALLBACK,
  STATUS_STYLES,
  type Dashboard as DashboardData,
  type EmailRecord,
  type Override,
  type ReviewReason,
  type StreamItem,
  type VerdictStatus,
  toSubmission,
} from "@/lib/types";
import type { Scoreboard } from "@/lib/data";

/** Official contract token → console label (the raw token stays in the payload preview). */
const STATUS_LABEL: Record<VerdictStatus, string> = {
  OK: "MATCHED",
  MISMATCH: "MISMATCHED",
  NEEDS_REVIEW: "NEEDS REVIEW",
};

const STATUS_TABS: Array<{ key: VerdictStatus | "ALL"; label: string }> = [
  { key: "ALL", label: "All" },
  { key: "MISMATCH", label: "Mismatched" },
  { key: "NEEDS_REVIEW", label: "Escalated" },
  { key: "OK", label: "Matched" },
];

const CATEGORY_FILTERS = [
  "ALL",
  "BL_COMPARISON",
  "SI_REQUEST",
  "INVOICE_QUERY",
  "GENERAL",
  "SPAM",
];

const REASONS: ReviewReason[] = [
  "wrong_doc_type",
  "missing_attachment",
  "unreadable",
  "missing_value",
];

function StatusPill({ status }: { status: VerdictStatus }) {
  return (
    <span className={`pill ${STATUS_STYLES[status]}`} title={`Official contract value: ${status}`}>
      {STATUS_LABEL[status]}
    </span>
  );
}

function Kpi({
  label,
  value,
  hint,
  tone = "slate",
}: {
  label: string;
  value: string | number;
  hint?: string;
  tone?: "slate" | "red" | "blue" | "green";
}) {
  const tones = {
    slate: "text-slate-900",
    red: "text-defect-text",
    blue: "text-escalate-text",
    green: "text-ok-text",
  } as const;
  return (
    <div className="card px-4 py-3">
      <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-500">
        {label}
      </div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${tones[tone]}`}>{value}</div>
      {hint ? <div className="mt-0.5 text-[11px] text-slate-400">{hint}</div> : null}
    </div>
  );
}

export default function Dashboard({
  data,
  scoreboard,
}: {
  data: DashboardData;
  scoreboard: Scoreboard | null;
}) {
  const [statusFilter, setStatusFilter] = useState<VerdictStatus | "ALL">("MISMATCH");
  const [categoryFilter, setCategoryFilter] = useState("BL_COMPARISON");
  const [onlyDefect, setOnlyDefect] = useState(false);
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string>(() => {
    const first = data.emails.find((mail) => mail.status === "MISMATCH");
    return first?.email_id ?? data.emails[0]?.email_id ?? "";
  });
  const [overrides, setOverrides] = useState<Record<string, Override>>({});
  const [draftFields, setDraftFields] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [reviewer, setReviewer] = useState("reviewer@aprilasia.com");
  // The on-demand audit panel is collapsed by default: the read-only console works without a
  // backend, so a live demo first shows the full corpus and expands the panel only when the
  // reviewer wants to prove the AI path end to end.
  const [showUpload, setShowUpload] = useState(false);
  // Live-pull results: used only for list highlighting and badges. Values come straight from
  // the API response and never change a verdict.
  const [liveItems, setLiveItems] = useState<Record<string, StreamItem>>({});
  const [liveSeq, setLiveSeq] = useState(0);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return data.emails.filter((mail) => {
      if (statusFilter !== "ALL" && mail.status !== statusFilter) return false;
      if (categoryFilter !== "ALL" && mail.category !== categoryFilter) return false;
      if (onlyDefect && !mail.has_defect) return false;
      if (!needle) return true;
      return (
        mail.email_id.toLowerCase().includes(needle) ||
        mail.subject.toLowerCase().includes(needle) ||
        mail.from.toLowerCase().includes(needle)
      );
    });
  }, [data.emails, statusFilter, categoryFilter, onlyDefect, query]);

  const selected: EmailRecord | undefined =
    data.emails.find((mail) => mail.email_id === selectedId) ?? filtered[0];
  const override = selected ? overrides[selected.email_id] : undefined;
  const submission = selected ? toSubmission(selected, override) : null;
  const activeFields = draftFields.length
    ? draftFields
    : override?.defect_fields ?? selected?.defect_fields ?? [];

  function pick(mail: EmailRecord) {
    setSelectedId(mail.email_id);
    setDraftFields([]);
    setNote("");
  }

  function apply(status: VerdictStatus) {
    if (!selected) return;
    const reason: ReviewReason | null =
      status === "NEEDS_REVIEW" ? selected.review_reason ?? "missing_value" : null;
    setOverrides((prev) => ({
      ...prev,
      [selected.email_id]: {
        status,
        defect_fields: status === "MISMATCH" ? [...activeFields].sort() : [],
        review_reason: reason,
        note,
        reviewer,
        at: new Date().toISOString(),
      },
    }));
    setDraftFields([]);
  }

  function clearOverride() {
    if (!selected) return;
    setOverrides((prev) => {
      const next = { ...prev };
      delete next[selected.email_id];
      return next;
    });
    setDraftFields([]);
  }

  function exportOverrides() {
    const payload = Object.fromEntries(
      Object.entries(overrides).map(([emailId, value]) => [
        emailId,
        {
          status: value.status,
          defect_fields: value.defect_fields,
          review_reason: value.review_reason,
          note: value.note,
          reviewer: value.reviewer,
        },
      ]),
    );
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "manual_overrides.json";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  const summary = data.summary;
  const overrideCount = Object.keys(overrides).length;

  return (
    <main className="mx-auto max-w-[1560px] px-5 py-5">
      <header className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">
            SDOC · SI vs BL Verification Console
          </h1>
          <p className="mt-0.5 text-xs text-slate-500">
            Classify → Extract → Deterministic Compare → Human Escalation
            <span className="mx-1.5 text-slate-300">|</span>
            Snapshot {data.generated_at.slice(0, 19).replace("T", " ")} UTC
          </p>
        </div>
        {/*
          Header cluster: every control is a `.header-control` (h-9 · rounded-md · shared padding)
          inside one `items-center` flex row, so the right edge is one straight horizontal line.
        */}
        <div className="flex flex-wrap items-center gap-2">
          <button
            className="header-control"
            onClick={() => setShowUpload((value) => !value)}
            aria-pressed={showUpload}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${showUpload ? "bg-emerald-500" : "bg-slate-300"}`}
            />
            {showUpload ? "Hide On-Demand Audit" : "On-Demand Audit (Real-time)"}
          </button>
          <span
            className="header-control header-control-muted"
            title="Share of emails decided by the expert-rule path with zero generative calls"
          >
            Expert Rules Path: {summary.rule_pct}%
          </span>
          {scoreboard?.final_score !== undefined ? (
            <span
              className="header-control header-control-dark"
              title="Official grading score produced by the organiser scorer"
            >
              Official Grading: {scoreboard.final_score.toFixed(4)}
            </span>
          ) : null}
          <button
            className="header-control"
            onClick={exportOverrides}
            disabled={!overrideCount}
            title="Export the reviewer override pack for re-submission"
          >
            Export Overrides ({overrideCount})
          </button>
        </div>
      </header>

      {showUpload ? <UploadPanel /> : null}

      <LiveStream
        onPick={(emailId) => {
          setSelectedId(emailId);
          setDraftFields([]);
          setNote("");
        }}
        onBatch={(items) => {
          setLiveItems((prev) => ({
            ...prev,
            ...Object.fromEntries(items.map((item) => [item.email_id, item])),
          }));
          setLiveSeq((value) => value + 1);
        }}
      />

      <RoiCards roi={data.roi} />

      <section className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Kpi label="Emails in Corpus" value={summary.total} hint={`${summary.with_attachments} with attachments`} />
        <Kpi label="Audit Targets" value={summary.bl_comparison} hint="SI vs draft BL" />
        <Kpi label="Field Mismatches" value={summary.mismatches} tone="red" hint="Graded end-to-end" />
        <Kpi label="Human Escalation" value={summary.needs_review} tone="blue" hint="Evidence insufficient" />
        <Kpi
          label="Matched"
          value={summary.statuses.OK ?? 0}
          tone="green"
          hint={`GENERAL ${summary.categories.GENERAL ?? 0} · SPAM ${summary.categories.SPAM ?? 0}`}
        />
        <Kpi
          label="Official Grading"
          value={scoreboard?.final_score?.toFixed(4) ?? "—"}
          hint={
            scoreboard
              ? `Stage-1 ${scoreboard.stage1_macro_f1?.toFixed(3) ?? "—"} · Stage-3 ${
                  scoreboard.stage3_defect_f1?.toFixed(3) ?? "—"
                }`
              : "Run test_submit to populate"
          }
        />
      </section>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[400px_1fr]">
        {/* ---------------- Left: audit queue ---------------- */}
        <section className="card flex max-h-[calc(100vh-230px)] flex-col overflow-hidden">
          <div className="space-y-2 border-b border-slate-200 p-3">
            <div className="flex flex-wrap gap-1">
              {STATUS_TABS.map((tab) => (
                <button
                  key={tab.key}
                  onClick={() => setStatusFilter(tab.key)}
                  className={`btn ${statusFilter === tab.key ? "btn-dark" : ""}`}
                >
                  {tab.label}
                  <span className="tabular-nums opacity-70">
                    {tab.key === "ALL" ? summary.total : summary.statuses[tab.key] ?? 0}
                  </span>
                </button>
              ))}
            </div>
            <div className="flex gap-2">
              <select
                className="flex-1 rounded-md border border-slate-300 px-2 py-1.5 text-xs"
                value={categoryFilter}
                onChange={(event) => setCategoryFilter(event.target.value)}
              >
                {CATEGORY_FILTERS.map((option) => (
                  <option key={option} value={option}>
                    {option === "ALL"
                      ? "All categories"
                      : `${option} (${summary.categories[option] ?? 0})`}
                  </option>
                ))}
              </select>
              <label className="btn" title="Show only email pairs with at least one field mismatch">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-slate-900"
                  checked={onlyDefect}
                  onChange={(event) => setOnlyDefect(event.target.checked)}
                />
                Mismatches only
              </label>
            </div>
            <input
              className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-xs"
              placeholder="Search email ID, subject or sender…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <p className="text-[11px] text-slate-400">{filtered.length} email(s) in view</p>
          </div>

          <ul className="scroll-thin flex-1 divide-y divide-slate-100 overflow-y-auto">
            {filtered.slice(0, 250).map((mail) => {
              const active = mail.email_id === (selected?.email_id ?? "");
              const liveItem = liveItems[mail.email_id];
              return (
                // The key carries the batch sequence so freshly audited rows remount and replay
                // the highlight animation exactly once.
                <li
                  key={`${mail.email_id}-${liveItem ? liveSeq : 0}`}
                  className={liveItem ? "flash-row" : undefined}
                >
                  <button
                    onClick={() => pick(mail)}
                    className={`w-full px-3 py-2 text-left transition ${
                      active
                        ? "bg-slate-900/[0.04] ring-1 ring-inset ring-slate-900/10"
                        : "hover:bg-slate-50"
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <StatusPill status={mail.status} />
                      <span className={`pill border-transparent ${CATEGORY_STYLES[mail.category]}`}>
                        {mail.category}
                      </span>
                      <span className="mono-cell ml-auto text-slate-400">{mail.email_id}</span>
                      {liveItem ? (
                        <span
                          className="pill border-emerald-300 bg-emerald-50 text-emerald-700"
                          title={`Audited by live pull: ${liveItem.duration_ms}ms, ${liveItem.trace.length}-step trace`}
                        >
                          ⚡ {liveItem.duration_ms}ms
                        </span>
                      ) : null}
                      {overrides[mail.email_id] ? (
                        <span className="pill border-amber-300 bg-amber-50 text-amber-700">
                          Overridden
                        </span>
                      ) : null}
                    </div>
                    <div className="mt-1 line-clamp-2 text-xs text-slate-600">{mail.subject}</div>
                    {mail.defect_fields.length ? (
                      <div className="mono-cell mt-1 text-defect-text">
                        {mail.defect_fields.join(" · ")}
                      </div>
                    ) : mail.review_reason ? (
                      <div className="mono-cell mt-1 text-escalate-text">
                        {REASON_FALLBACK[mail.review_reason] ?? mail.review_reason}
                      </div>
                    ) : null}
                  </button>
                </li>
              );
            })}
            {filtered.length === 0 ? (
              <li className="p-4 text-sm text-slate-500">No email matches the current filters.</li>
            ) : null}
          </ul>
        </section>

        {/* ---------------- Right: evidence + human override ---------------- */}
        <section className="space-y-4">
          {selected ? (
            <>
              <div className="card p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusPill status={submission?.status ?? selected.status} />
                  <span className={`pill border-transparent ${CATEGORY_STYLES[selected.category]}`}>
                    {selected.category}
                  </span>
                  <span className="mono-cell text-slate-400">{selected.email_id}</span>
                  <span
                    className="mono-cell ml-auto text-slate-500"
                    title="Cadence that produced this verdict, with rule name and confidence"
                  >
                    {selected.decided_by === "rule" ? "Expert rules" : "Generative model"}
                    {selected.rule_name ? ` · ${selected.rule_name}` : ""}
                    {selected.category_confidence !== null
                      ? ` · p=${selected.category_confidence}`
                      : ""}
                  </span>
                </div>
                <h2 className="mt-2 text-sm font-semibold text-slate-800">{selected.subject}</h2>
                <p className="mono-cell text-slate-500">
                  {selected.from} → {selected.attachments.join(", ") || "(no attachment)"}
                </p>
                {selected.review_reason_label ? (
                  <p className="mt-2 rounded-md border border-escalate-border bg-escalate-bg px-2.5 py-1.5 text-xs text-escalate-text">
                    Escalated: {selected.review_reason_label}
                  </p>
                ) : null}
              </div>

              <div className="card p-4">
                <div className="mb-2 flex items-center gap-2">
                  <h3 className="text-sm font-semibold text-slate-800">
                    Field-by-field comparison
                  </h3>
                  <span className="text-[11px] text-slate-400">
                    7 canonical fields · tick a field to add it to the defect set
                  </span>
                </div>
                <FieldDiff
                  fields={selected.fields}
                  selected={activeFields}
                  onToggle={(field) =>
                    setDraftFields((prev) => {
                      const base = prev.length ? prev : selected.defect_fields;
                      return base.includes(field)
                        ? base.filter((item) => item !== field)
                        : [...base, field];
                    })
                  }
                />
              </div>

              <AgentTrace
                emailId={selected.email_id}
                steps={selected.trace ?? []}
                totalMs={selected.trace_total_ms}
                skipped={selected.trace_skipped_agents ?? []}
                defaultOpen={selected.status !== "OK"}
              />

              {/* items-stretch + h-full keeps both cards' bottom edges on one baseline */}
              <div className="grid grid-cols-1 items-stretch gap-4 lg:grid-cols-2">
                <div className="card flex flex-col p-4">
                  <h3
                    className="mb-2 text-sm font-semibold text-slate-800"
                    title="Persisted through the apply_manual_verdict RPC, so the submission view reflects it without any change to the scoring code"
                  >
                    Human-in-the-Loop Override
                  </h3>
                  <div className="flex flex-wrap gap-2">
                    <button
                      className="btn border-defect-border bg-defect-bg text-defect-text"
                      onClick={() => apply("MISMATCH")}
                      disabled={activeFields.length === 0}
                    >
                      Confirm mismatch ({activeFields.length} field(s))
                    </button>
                    <button
                      className="btn border-ok-border bg-ok-bg text-ok-text"
                      onClick={() => apply("OK")}
                    >
                      Override to matched
                    </button>
                    <button
                      className="btn border-escalate-border bg-escalate-bg text-escalate-text"
                      onClick={() => apply("NEEDS_REVIEW")}
                    >
                      Escalate to review
                    </button>
                    <button className="btn" onClick={clearOverride} disabled={!override}>
                      Revert override
                    </button>
                  </div>
                  <div className="mt-3 space-y-2">
                    <select
                      className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-xs"
                      value={selected.review_reason ?? "missing_value"}
                      onChange={() => undefined}
                      disabled
                      title="Escalation reason — set by the escalation ladder, not by the reviewer"
                    >
                      {REASONS.map((reason) => (
                        <option key={reason} value={reason}>
                          {data.reason_labels[reason] ?? REASON_FALLBACK[reason] ?? reason}
                        </option>
                      ))}
                    </select>
                    <textarea
                      className="h-16 w-full rounded-md border border-slate-300 px-2 py-1.5 text-xs"
                      placeholder="Reviewer note (exported with the override pack)"
                      value={note}
                      onChange={(event) => setNote(event.target.value)}
                    />
                    <input
                      className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-xs"
                      value={reviewer}
                      onChange={(event) => setReviewer(event.target.value)}
                      placeholder="Reviewer"
                    />
                  </div>
                  {override ? (
                    <p className="mt-3 rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-[11px] text-amber-800">
                      Overridden: {STATUS_LABEL[override.status]} ·{" "}
                      {override.defect_fields.join(", ") || "no defect fields"} ·{" "}
                      {override.note || "no note"}
                    </p>
                  ) : null}
                </div>

                <div className="card flex flex-col p-4">
                  <div className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1">
                    <h3 className="text-sm font-semibold text-slate-800">
                      Submission Payload Preview
                    </h3>
                    <span className="whitespace-nowrap text-[11px] text-slate-400">
                      5 contract keys · overrides applied
                    </span>
                  </div>
                  <pre className="scroll-thin min-h-[12rem] flex-1 overflow-auto rounded-md bg-slate-900 p-3 text-[11px] leading-5 text-emerald-300">
{JSON.stringify({ [selected.email_id]: submission }, null, 2)}
                  </pre>
                </div>
              </div>

              <div className="card p-4">
                <h3 className="mb-2 text-sm font-semibold text-slate-800">Email body</h3>
                <pre className="scroll-thin max-h-48 overflow-auto whitespace-pre-wrap rounded-md border border-slate-200 bg-slate-50 p-3 text-[11px] leading-5 text-slate-600">
{selected.body}
                </pre>
              </div>
            </>
          ) : (
            <p className="card p-6 text-sm text-slate-500">
              Select an email from the audit queue to review it.
            </p>
          )}
        </section>
      </div>
    </main>
  );
}
