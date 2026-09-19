/** 工作台的数据契约 —— 与后端 app/dashboard.py 的导出结构逐字对应。 */

export type VerdictStatus = "OK" | "MISMATCH" | "NEEDS_REVIEW";
export type Category =
  | "BL_COMPARISON"
  | "SI_REQUEST"
  | "INVOICE_QUERY"
  | "GENERAL"
  | "SPAM";
export type ReviewReason =
  | "wrong_doc_type"
  | "missing_attachment"
  | "unreadable"
  | "missing_value";

export type FieldVerdict = "match" | "defect" | "undecided";

export interface FieldRow {
  field: string;
  label: string;
  si_raw: string | null;
  bl_raw: string | null;
  si_normalized: string | null;
  bl_normalized: string | null;
  is_match: boolean;
  match_method: string;
  similarity: number | null;
  delta: number | null;
  needs_human: boolean;
  needs_human_reason: ReviewReason | null;
  rationale: string | null;
  detail: Record<string, unknown>;
  verdict: FieldVerdict;
}

export interface DocSide {
  path: string;
  file: string;
  readable: boolean;
  read_error: string | null;
  doc_role: string;
  other_doc_kind: string | null;
  blank_fields: string[];
  values: Record<string, string | null>;
}

export interface EmailRecord {
  email_id: string;
  from: string;
  subject: string;
  body: string;
  attachments: string[];
  attachment_count: number;
  category: Category;
  status: VerdictStatus;
  has_defect: boolean;
  defect_fields: string[];
  review_reason: ReviewReason | null;
  review_reason_label: string | null;
  decided_by: string;
  rule_name: string | null;
  category_confidence: number | null;
  body_hint: ReviewReason | null;
  si: DocSide | null;
  bl: DocSide | null;
  fields: FieldRow[];
  /** 该封邮件四步 Agent 轨迹（导出时实测耗时与证据，与红框同源）。 */
  trace?: AgentStep[];
  trace_total_ms?: number;
  trace_skipped_agents?: string[];
}

/**
 * 多 Agent 单步执行记录。
 *
 * ★ 字段与后端 `agents.base.AgentStep.as_dict()` 逐键一致，因此**两份来源**
 *   （跑批导出的快照 / /api/agents/trace 现算）能共用同一个渲染器：
 *   `agent` 是机器 id（triage / extractor / cross_verifier / escalation_judge），
 *   `role` 是两者共同的连接键，展示名在前端映射（见 AGENT_DISPLAY）。
 */
export interface AgentStep {
  agent: string;
  role: string;
  duration_ms: number;
  summary: string;
  used_llm: boolean;
  model: string | null;
  error: string | null;
  evidence: Record<string, unknown>;
}

export interface AgentTrace {
  steps: AgentStep[];
  total_ms?: number;
  llm_calls?: number;
  agents?: string[];
  errors?: string[];
  skipped?: string[];
}

/** 展示层映射：机器 id / role → 人看的名字与配色。 */
export const AGENT_DISPLAY: Record<string, { name: string; role: string; badge: string }> = {
  triage: {
    name: "TriageAgent",
    role: "Expert-rule routing, LLM fallback",
    badge: "border-sky-200 bg-sky-50 text-sky-700",
  },
  extractor: {
    name: "ExtractorAgent",
    role: "Multimodal extraction · 7 canonical fields",
    badge: "border-violet-200 bg-violet-50 text-violet-700",
  },
  verifier: {
    name: "CrossVerifierAgent",
    role: "Deterministic judgement matrix (zero randomness)",
    badge: "border-emerald-200 bg-emerald-50 text-emerald-700",
  },
  judge: {
    name: "EscalationJudgeAgent",
    role: "Escalation ladder ruling",
    badge: "border-amber-200 bg-amber-50 text-amber-700",
  },
};

/** Comparison method → human label shown in the verdict column. */
export const METHOD_LABELS: Record<string, string> = {
  exact: "Exact",
  normalized: "Normalized",
  alias: "Alias",
  numeric_tolerance: "Numeric tolerance",
  fuzzy: "Fuzzy",
  missing: "Missing",
  blank_one_side: "One-sided blank",
  not_compared: "Not compared",
};

/** Escalation reason → short English label for the review queue. */
export const REASON_FALLBACK: Record<string, string> = {
  wrong_doc_type: "Attachment is not a draft BL",
  missing_attachment: "Comparable document missing",
  unreadable: "No readable text layer (likely a scan)",
  missing_value: "Blank or placeholder values",
};

/** GBS ROI：三张大卡的唯一数据源（与后端 analytics.RoiReport 对应）。 */
export interface RoiReport {
  emails_total: number;
  pairs_reviewed: number;
  rule_decided: number;
  llm_decided: number;
  defects_caught: number;
  manually_escalated: number;
  machine_seconds: number;
  human_seconds: number;
  time_saved_seconds: number;
  time_saved_hours: number;
  time_saved_ratio: number;
  llm_calls_avoided: number;
  tokens_avoided: number;
  token_cost_saved_usd: number;
  token_cost_if_all_llm_usd: number;
  labor_cost_saved_usd: number;
  batch_scale: number;
  monthly_token_cost_saved_usd: number;
  annual_token_cost_saved_usd: number;
  annual_labor_cost_saved_usd: number;
  annual_total_saved_usd: number;
  assumptions: Record<string, number>;
  provenance: Record<string, string | Record<string, number>>;
}

/** /api/stream/pull 的单封结果。 */
export interface StreamItem {
  email_id: string;
  subject: string;
  from: string;
  attachments: string[];
  category: Category;
  status: VerdictStatus;
  has_defect: boolean;
  defect_fields: string[];
  review_reason: ReviewReason | null;
  duration_ms: number;
  trace: AgentStep[];
}

export interface StreamBatch {
  items: StreamItem[];
  cursor: number;
  pool_size: number;
  batch_size: number;
  duration_ms: number;
  defect_count: number;
  channel: "gemini" | "deterministic";
  read_only: boolean;
}

export interface DashboardSummary {
  total: number;
  categories: Record<string, number>;
  statuses: Record<string, number>;
  review_reasons: Record<string, number>;
  defect_fields: Record<string, number>;
  mismatches: number;
  needs_review: number;
  bl_comparison: number;
  rule_decided: number;
  rule_pct: number;
  with_attachments: number;
}

export interface Dashboard {
  generated_at: string;
  summary: DashboardSummary;
  /** 三张 ROI 大卡的数据源；老快照可能没有（前端会优雅隐藏）。 */
  roi?: RoiReport;
  field_labels: Record<string, string>;
  reason_labels: Record<string, string>;
  emails: EmailRecord[];
}

/** 官方提交产物的单条记录：恰好 5 个键。 */
export interface SubmissionRecord {
  category: Category;
  status: VerdictStatus;
  review_reason: ReviewReason | null;
  has_defect: boolean;
  defect_fields: string[];
}

/** 人审在界面上的改判（会覆盖机器结论，并实时反映到提交预览里）。 */
export interface Override {
  status: VerdictStatus;
  defect_fields: string[];
  review_reason: ReviewReason | null;
  note: string;
  reviewer: string;
  at: string;
}

export const STATUS_STYLES: Record<VerdictStatus, string> = {
  MISMATCH: "bg-defect-bg text-defect-text border-defect-border",
  NEEDS_REVIEW: "bg-escalate-bg text-escalate-text border-escalate-border",
  OK: "bg-ok-bg text-ok-text border-ok-border",
};

export const CATEGORY_STYLES: Record<string, string> = {
  BL_COMPARISON: "bg-slate-900 text-white",
  SI_REQUEST: "bg-slate-200 text-slate-700",
  INVOICE_QUERY: "bg-slate-200 text-slate-700",
  GENERAL: "bg-slate-100 text-slate-500",
  SPAM: "bg-rose-100 text-rose-700",
};

/** 按官方合约把一条记录折算成提交产物（人工覆盖优先）。 */
export function toSubmission(
  record: EmailRecord,
  override?: Override,
): SubmissionRecord {
  const category = record.category;
  const status = override ? override.status : record.status;

  // 非对照类邮件在官方口径里永远是干净的 OK —— 这条归一必须与 SQL 视图一致
  if (category !== "BL_COMPARISON") {
    return {
      category,
      status: "OK",
      review_reason: null,
      has_defect: false,
      defect_fields: [],
    };
  }
  if (status === "MISMATCH") {
    return {
      category,
      status,
      review_reason: null,
      has_defect: true,
      defect_fields: [...(override ? override.defect_fields : record.defect_fields)].sort(),
    };
  }
  if (status === "NEEDS_REVIEW") {
    return {
      category,
      status,
      review_reason:
        (override ? override.review_reason : record.review_reason) ?? "missing_value",
      has_defect: false,
      defect_fields: [],
    };
  }
  return { category, status: "OK", review_reason: null, has_defect: false, defect_fields: [] };
}
