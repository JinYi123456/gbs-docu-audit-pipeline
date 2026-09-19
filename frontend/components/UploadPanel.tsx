"use client";

import { useRef, useState } from "react";

import FieldDiff from "@/components/FieldDiff";
import { getAiStatus, verifyUpload, type ApiResult } from "@/lib/api";
import {
  AGENT_DISPLAY,
  CATEGORY_STYLES,
  STATUS_STYLES,
  type AgentStep,
  type FieldRow,
  type VerdictStatus,
} from "@/lib/types";

/** Response of `/api/verify` (same shape as backend `upload.verify_uploaded_documents`). */
interface UploadRecord {
  email_id: string;
  run_id: string;
  subject: string;
  attachments: string[];
  category: string;
  status: VerdictStatus;
  has_defect: boolean;
  defect_fields: string[];
  review_reason: string | null;
  review_reason_label: string | null;
  rationale?: string;
  duration_ms: number;
  fields: FieldRow[];
  classification?: {
    detected_category: string;
    confidence: number;
    decided_by: string;
    rule_name?: string | null;
    model?: string | null;
    evidence_span?: string;
  };
  extractor?: {
    mode: string;
    model: string;
    llm_used: boolean;
    latency_ms: number;
    from_cache: boolean;
    error: string | null;
    si_readable: boolean;
    bl_readable: boolean;
  };
  agents?: { steps: AgentStep[]; total_ms: number; llm_calls: number; errors: string[] };
  storage?: Record<string, unknown>;
}

interface VerifyResponse {
  record: UploadRecord;
  ai: {
    requested: boolean;
    used: boolean;
    model: string | null;
    mode: string;
    detail: string | null;
  };
  storage: Record<string, unknown>;
}

const STATUS_LABEL: Record<VerdictStatus, string> = {
  OK: "MATCHED",
  MISMATCH: "MISMATCHED",
  NEEDS_REVIEW: "NEEDS REVIEW",
};

const SAMPLE_SI = `SHIPPING INSTRUCTION
Shipper: APRIL FINE PAPER TRADING (MIDDLE EAST) FZE
Consignee (Non-Negotiable): VITAL SOLUTIONS PTE. LTD.
NOTIFY PARTY: VITAL SOLUTIONS PTE. LTD.
PORT OF LOADING: NHAVA SHEVA, INDIA (INNSA)
Port of Discharge: MOMBASA, KENYA (KEMBA)
No. of Containers: 1 x 20'GP
Gross Wt (kgs): 21,114 KG`;

const SAMPLE_BL = `BILL OF LADING (DRAFT)
Shipper (Principal or Seller): APRIL FINE PAPER TRADING (MIDDLE EAST) FZE
CONSIGNEE: VITAL SOLUTIONS PTE. LTD.
Notify Party: VITAL SOLUTIONS PTE. LTD.
Port of Loading: NHAVA SHEVA, INDIA (INNSA)
POD: MOMBASA, KENYA (KEMBA)
Container Count: 3 x 20'GP
Gross Wt (kgs): 23,114 KG`;

function FileSlot({
  label,
  hint,
  file,
  onPick,
  onClear,
}: {
  label: string;
  hint: string;
  file: File | null;
  onPick: (file: File | null) => void;
  onClear: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  return (
    <div
      className={`rounded-lg border border-dashed px-3 py-3 text-xs transition ${
        file ? "border-emerald-300 bg-emerald-50/50" : "border-slate-300 bg-white"
      }`}
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
        const dropped = event.dataTransfer.files?.[0];
        if (dropped) onPick(dropped);
      }}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold text-slate-700">{label}</span>
        {file ? (
          <button className="text-slate-400 hover:text-slate-700" onClick={onClear}>
            Remove
          </button>
        ) : null}
      </div>
      <input
        ref={inputRef}
        type="file"
        className="hidden"
        accept=".pdf,.docx,.xlsx,.txt,.csv,.png,.jpg,.jpeg"
        onChange={(event) => onPick(event.target.files?.[0] ?? null)}
      />
      <button
        className="mt-1 w-full truncate rounded-md border border-slate-300 bg-slate-50 px-2 py-1.5 text-left text-[11px] text-slate-600 hover:border-slate-400"
        onClick={() => inputRef.current?.click()}
      >
        {file ? `${file.name} (${(file.size / 1024).toFixed(0)} KB)` : hint}
      </button>
    </div>
  );
}

export default function UploadPanel() {
  const [siFile, setSiFile] = useState<File | null>(null);
  const [blFile, setBlFile] = useState<File | null>(null);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [useLlm, setUseLlm] = useState(true);
  const [persistCloud, setPersistCloud] = useState(true);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<VerifyResponse | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  // Kept separate from `failure`: channel information is normal status, not an error.
  const [info, setInfo] = useState<string | null>(null);

  function pickText(text: string, name: string, setter: (f: File | null) => void) {
    setter(new File([text], name, { type: "text/plain" }));
  }

  async function run() {
    if (!siFile && !blFile) {
      setFailure("Select at least one document (SI or draft BL).");
      return;
    }
    setLoading(true);
    setFailure(null);
    const form = new FormData();
    if (siFile) form.append("files", siFile, siFile.name);
    if (blFile) form.append("files", blFile, blFile.name);
    form.append("subject", subject);
    form.append("body", body);
    form.append("use_llm", useLlm ? "true" : "false");
    form.append("persist_cloud", persistCloud ? "true" : "false");

    const response: ApiResult<VerifyResponse> = await verifyUpload<VerifyResponse>(form);
    if (response.ok) {
      setResult(response.data);
    } else {
      setResult(null);
      setFailure(`${response.message}${response.detail ? `\n${response.detail}` : ""}`);
    }
    setLoading(false);
  }

  async function probeAi() {
    const response = await getAiStatus();
    if (response.ok) {
      setInfo(`Model channel\n${JSON.stringify(response.data, null, 2)}`);
      return;
    }
    setInfo(null);
    setFailure(response.message);
  }

  const record = result?.record;
  const agents = record?.agents;

  return (
    <section className="card mb-4 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold text-slate-800">On-Demand Audit (real-time)</h2>
          <p className="mt-0.5 text-[11px] text-slate-500">
            Drop in an SI and a draft BL — classification, multimodal extraction of the 7 canonical
            fields, the deterministic comparison matrix and the escalation ruling all run on the
            same pipeline as the full corpus. Uploads are journalled separately and never touch the
            graded payload.
          </p>
        </div>
        <div className="flex items-center gap-3 text-[11px] text-slate-600">
          <label
            className="flex items-center gap-1"
            title="Perception only: extraction may use Gemini multimodal; the verdict layer stays deterministic"
          >
            <input
              type="checkbox"
              className="h-3.5 w-3.5 accent-slate-900"
              checked={useLlm}
              onChange={(event) => setUseLlm(event.target.checked)}
            />
            Gemini multimodal
          </label>
          <label className="flex items-center gap-1" title="Journal this run for audit trail">
            <input
              type="checkbox"
              className="h-3.5 w-3.5 accent-slate-900"
              checked={persistCloud}
              onChange={(event) => setPersistCloud(event.target.checked)}
            />
            Persist to cloud
          </label>
          <button className="btn" onClick={probeAi}>
            Inspect model channel
          </button>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-[1fr_1fr_1.2fr]">
        <FileSlot
          label="Shipping Instruction (baseline)"
          hint="Click to choose, or drop a file here"
          file={siFile}
          onPick={setSiFile}
          onClear={() => setSiFile(null)}
        />
        <FileSlot
          label="Draft Bill of Lading (under review)"
          hint="Click to choose, or drop a file here"
          file={blFile}
          onPick={setBlFile}
          onClear={() => setBlFile(null)}
        />
        <div className="space-y-2">
          <input
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-xs"
            placeholder="Email subject (optional — feeds routing and side-channel evidence)"
            value={subject}
            onChange={(event) => setSubject(event.target.value)}
          />
          <textarea
            className="h-14 w-full rounded-md border border-slate-300 px-2 py-1.5 text-xs"
            placeholder="Body (optional). e.g. “Some SI fields were left blank by the customer” routes blanks to human review instead of treating them as defects"
            value={body}
            onChange={(event) => setBody(event.target.value)}
          />
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button className="btn btn-dark" onClick={run} disabled={loading}>
          {loading ? "Auditing…" : "Run audit"}
        </button>
        <button className="btn" onClick={() => pickText(SAMPLE_SI, "demo_SI.txt", setSiFile)}>
          Load sample SI
        </button>
        <button className="btn" onClick={() => pickText(SAMPLE_BL, "demo_BL.txt", setBlFile)}>
          Load sample BL (mismatched containers)
        </button>
      </div>

      {failure ? (
        <pre className="scroll-thin mt-3 max-h-40 overflow-auto whitespace-pre-wrap rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-900">
{failure}
        </pre>
      ) : null}

      {info ? (
        <pre className="scroll-thin mt-3 max-h-64 overflow-auto whitespace-pre-wrap rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-[11px] text-slate-700">
{info}
        </pre>
      ) : null}

      {record ? (
        <div className="mt-4 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`pill ${STATUS_STYLES[record.status]}`}>
              {STATUS_LABEL[record.status]}
            </span>
            <span className={`pill border-transparent ${CATEGORY_STYLES[record.category]}`}>
              On-demand audit
            </span>
            <span className="pill border-slate-300 bg-white text-slate-600">
              {/* 型号名本身已带厂商前缀（gemini-3.6-flash），不再拼一次 "Gemini" */}
              {record.extractor?.llm_used
                ? `${record.extractor.model} · ${record.extractor.mode}`
                : "Deterministic path"}
            </span>
            {record.extractor?.from_cache ? (
              <span className="pill border-slate-300 bg-white text-slate-500">Cache hit</span>
            ) : null}
            <span className="mono-cell text-[11px] text-slate-400">
              {record.duration_ms}ms · run {record.run_id.slice(0, 8)}
            </span>
            <span className="mono-cell ml-auto text-[11px] text-slate-500">
              {record.defect_fields.length
                ? `Defect fields: ${record.defect_fields.join(", ")}`
                : "No defect fields"}
            </span>
          </div>

          {record.review_reason_label ? (
            <p className="rounded-md border border-escalate-border bg-escalate-bg px-2.5 py-1.5 text-xs text-escalate-text">
              Escalation reason: {record.review_reason_label}
            </p>
          ) : null}

          <FieldDiff fields={record.fields} selected={record.defect_fields} />

          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
              <h3 className="text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-500">
                Multi-Agent Execution Trace
              </h3>
              <ul className="mt-2 space-y-1">
                {(agents?.steps ?? []).map((step, index) => {
                  const display = AGENT_DISPLAY[step.role];
                  return (
                    <li key={`${step.agent}-${index}`} className="flex gap-2 text-[11px]">
                      <span className="mono-cell w-14 shrink-0 text-slate-400">
                        {step.duration_ms}ms
                      </span>
                      <span
                        className={`pill border-transparent ${
                          step.used_llm ? "bg-slate-900 text-white" : "bg-slate-200 text-slate-700"
                        }`}
                      >
                        {step.used_llm ? "Gemini" : "Rules"}
                      </span>
                      <span className="shrink-0 font-semibold text-slate-700">
                        {display?.name ?? step.agent}
                      </span>
                      <span className="truncate text-slate-500" title={step.summary}>
                        {step.summary}
                      </span>
                      {step.error ? (
                        <span className="text-defect-text">({step.error})</span>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
              {agents?.errors?.length ? (
                <p className="mt-2 text-[11px] text-defect-text">
                  Errors: {agents.errors.join("; ")}
                </p>
              ) : null}
            </div>
            <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
              <h3 className="text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-500">
                Persistence &amp; Model Channel
              </h3>
              <dl className="mt-2 space-y-1 text-[11px]">
                {[
                  ["Extraction mode", record.extractor?.mode],
                  ["Model", record.extractor?.llm_used ? record.extractor?.model : "—"],
                  ["Extraction latency", record.extractor ? `${record.extractor.latency_ms}ms` : null],
                  ["Routing", record.classification?.rule_name ?? record.classification?.decided_by],
                  ["Journal", (record.storage as { transport?: string } | undefined)?.transport],
                ]
                  .filter(([, value]) => value !== undefined && value !== null && value !== "")
                  .map(([key, value]) => (
                    <div key={String(key)} className="flex gap-2">
                      <dt className="w-36 shrink-0 text-slate-500">{key}</dt>
                      <dd className="mono-cell text-slate-700">{String(value)}</dd>
                    </div>
                  ))}
              </dl>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
