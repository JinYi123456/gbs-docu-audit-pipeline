"use client";

import { useState } from "react";

import { pullStream } from "@/lib/api";
import {
  CATEGORY_STYLES,
  STATUS_STYLES,
  type StreamBatch,
  type StreamItem,
  type VerdictStatus,
} from "@/lib/types";

/**
 * "⚡ Fetch Latest GBS Mail" — the button that opens a live demo.
 *
 * Where the line sits between real and staged (worth stating out loud when asked):
 *   · Real: the click POSTs to /api/stream/pull and the backend genuinely runs
 *     classify → extract → compare → rule for a fresh slice of the corpus.
 *   · Staged: the 260 ms stagger before each card flips from PROCESSING to its verdict is a
 *     presentation choice — the deterministic path finishes a six-email batch in under a second.
 *   · Every email ID, defect field and duration on the cards comes from that real response.
 *
 * Read-only by construction: the endpoint never writes back to `emails` / `submission_view`, so a
 * demo cannot disturb the graded payload (pinned by regression tests).
 */

const STATUS_LABEL: Record<VerdictStatus, string> = {
  OK: "MATCHED",
  MISMATCH: "MISMATCHED",
  NEEDS_REVIEW: "NEEDS REVIEW",
};

interface Phase {
  item: StreamItem;
  state: "processing" | "done";
}

export default function LiveStream({
  onPick,
  onBatch,
}: {
  /** Open an audited email in the detailed comparison view */
  onPick?: (emailId: string) => void;
  /** Publish the batch so the audit queue can highlight the same rows */
  onBatch?: (items: StreamItem[]) => void;
}) {
  const [running, setRunning] = useState(false);
  const [batch, setBatch] = useState<StreamBatch | null>(null);
  const [phases, setPhases] = useState<Phase[]>([]);
  const [note, setNote] = useState<string | null>(null);

  async function pull(reset: boolean) {
    setRunning(true);
    setNote(null);
    const response = await pullStream(6, { reset });
    if (!response.ok) {
      setNote(response.message.split("\n")[0]);
      setRunning(false);
      return;
    }
    const data = response.data;
    setBatch(data);
    // Stage the reveal so the audience can see work happening; the verdicts are already computed.
    setPhases(data.items.map((item) => ({ item, state: "processing" as const })));
    onBatch?.(data.items);
    data.items.forEach((item, index) => {
      setTimeout(() => {
        setPhases((prev) =>
          prev.map((phase) =>
            phase.item.email_id === item.email_id ? { ...phase, state: "done" } : phase,
          ),
        );
      }, 260 * (index + 1));
    });
    setTimeout(() => setRunning(false), 260 * (data.items.length + 1));
  }

  const doneCount = phases.filter((phase) => phase.state === "done").length;

  return (
    <section className="mb-4 overflow-hidden rounded-xl border border-slate-800 bg-slate-950 text-slate-100 shadow-sm">
      <div className="flex flex-wrap items-center gap-3 px-4 py-3">
        <button
          className={`relative inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-semibold transition ${
            running
              ? "border-amber-400/50 bg-amber-400/10 text-amber-200"
              : "border-emerald-400/50 bg-emerald-400/10 text-emerald-200 hover:bg-emerald-400/20"
          }`}
          onClick={() => pull(false)}
          disabled={running}
          title="Runs the full pipeline on the next batch of pending SI / draft-BL pairs"
        >
          <span className="relative flex h-2.5 w-2.5">
            {running ? (
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-80" />
            ) : null}
            <span
              className={`relative inline-flex h-2.5 w-2.5 rounded-full ${
                running ? "bg-amber-400" : "bg-emerald-400"
              }`}
            />
          </span>
          {running ? "Auditing…" : "⚡ Fetch Latest GBS Mail"}
        </button>

        <button
          className="btn border-slate-700 bg-slate-900 text-slate-300"
          onClick={() => pull(true)}
          disabled={running}
          title="Reset the pool cursor and start the rotation from the first audit target"
        >
          Restart stream
        </button>

        {batch ? (
          <span className="font-mono text-[11px] text-slate-400">
            Pool {batch.pool_size} · cursor {batch.cursor} · batch {batch.batch_size} ·{" "}
            {batch.duration_ms}ms · defects{" "}
            <span className="font-semibold text-rose-300">{batch.defect_count}</span> ·{" "}
            {batch.channel === "gemini" ? "multimodal path" : "deterministic path"}
          </span>
        ) : (
          <span className="font-mono text-[11px] text-slate-500">
            Read-only audit · graded payload is never modified
          </span>
        )}

        {batch ? (
          <span className="ml-auto rounded border border-emerald-400/30 bg-emerald-400/10 px-1.5 py-0.5 font-mono text-[10px] text-emerald-300">
            real-time API connected
          </span>
        ) : null}
      </div>

      {note ? (
        <p className="border-t border-amber-400/30 bg-amber-400/10 px-4 py-2 text-[11px] text-amber-200">
          {note}
        </p>
      ) : null}

      {phases.length ? (
        <ul className="grid grid-cols-1 gap-2 border-t border-white/5 px-4 py-3 sm:grid-cols-2 xl:grid-cols-3">
          {phases.map((phase, index) => {
            const { item } = phase;
            const pending = phase.state === "processing";
            return (
              <li
                key={item.email_id}
                className={`card-pop rounded-lg border p-2.5 transition ${
                  pending
                    ? "border-white/10 bg-white/5"
                    : item.has_defect
                      ? "border-rose-400/40 bg-rose-500/10"
                      : item.status === "NEEDS_REVIEW"
                        ? "border-amber-400/40 bg-amber-500/10"
                        : "border-emerald-400/40 bg-emerald-500/10"
                }`}
                style={{ animationDelay: `${index * 40}ms` }}
              >
                <div className="flex items-center gap-2">
                  <span className="font-mono text-[11px] text-slate-300">{item.email_id}</span>
                  {pending ? (
                    <span className="flex items-center gap-1 rounded border border-amber-400/40 bg-amber-400/10 px-1.5 py-0.5 font-mono text-[10px] text-amber-200">
                      <span className="h-1.5 w-1.5 animate-ping rounded-full bg-amber-300" />
                      PROCESSING
                    </span>
                  ) : (
                    <span className={`pill ${STATUS_STYLES[item.status]}`}>
                      {STATUS_LABEL[item.status]}
                    </span>
                  )}
                  <span className={`pill border-transparent ${CATEGORY_STYLES[item.category]}`}>
                    {item.category}
                  </span>
                  {!pending ? (
                    <button
                      className="ml-auto whitespace-nowrap text-[10px] font-semibold text-slate-200 underline decoration-dotted"
                      onClick={() => onPick?.(item.email_id)}
                    >
                      View diff
                    </button>
                  ) : null}
                </div>
                <p className="mt-1 line-clamp-2 text-[11px] leading-4 text-slate-300">
                  {item.subject}
                </p>
                {pending ? (
                  <p className="mt-1 font-mono text-[10px] text-slate-400">
                    classify → extract → compare → rule …
                  </p>
                ) : (
                  <>
                    <p className="mt-1 font-mono text-[10px] text-slate-400">
                      {item.trace.length} steps · {item.duration_ms}ms ·{" "}
                      {item.attachments.length} attachment(s)
                    </p>
                    {item.defect_fields.length ? (
                      <p className="mt-1 font-mono text-[10px] text-rose-300">
                        MISMATCHED: {item.defect_fields.join(" · ")}
                      </p>
                    ) : item.review_reason ? (
                      <p className="mt-1 font-mono text-[10px] text-amber-200">
                        Escalated: {item.review_reason.replace(/_/g, " ")}
                      </p>
                    ) : (
                      <p className="mt-1 font-mono text-[10px] text-emerald-300">
                        All 7 fields matched
                      </p>
                    )}
                  </>
                )}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="border-t border-white/5 px-4 py-2 font-mono text-[11px] text-slate-400">
          Fetch the next batch of pending SI / draft-BL pairs and watch each verdict land as the
          pipeline completes.
        </p>
      )}

      {phases.length && doneCount < phases.length ? (
        <p className="border-t border-white/5 px-4 py-1.5 text-right font-mono text-[10px] text-slate-500">
          verdicts returned {doneCount}/{phases.length}
        </p>
      ) : null}
    </section>
  );
}
