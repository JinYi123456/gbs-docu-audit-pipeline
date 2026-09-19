"""人审工作台数据导出 —— 前端只读这一个 JSON 就能画完整界面。

为什么要有这层：前端不该依赖「后端进程恰好在跑」或「Supabase 恰好配好了」。
把「邮件元信息 + SI/BL 原文 + 7 字段比对明细 + 升级理由」压成一个静态快照，
于是：
    · 演示前断网 / 没起后端，工作台照样能开；
    · 任何人拿到这个文件都能复现同一屏内容（逐比特可重放）；
    · 前端零密钥，service-role key 永远不出后端。

每封邮件的字段明细在导出时重新走一遍确定性链路（load_slots → extract_rule →
compare_documents），保证快照里展示的就是判定矩阵的真实输入与真实输出，
而不是另写一套"展示用"逻辑 —— 展示与判定同源，才不会出现界面和分数不一致。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .analytics import compute_roi
from .config import DEFAULT_DASHBOARD_PATH, resolve_data_dir
from .ingest.inbox import InboxSource
from .ingest.readers import read_document
from .pipeline.classify import rule_classify
from .pipeline.compare import compare_documents
from .pipeline.extract import ROLE_BL, ROLE_SI, DocSlot, split_attachments
from .pipeline.normalize import COMPARE_FIELDS
from .pipeline.rule_extract import extract_document_view

# 前端的字段中文名（与官方字段一一对应，仅用于展示）
# Display labels for the console (English-only delivery). Keys mirror the canonical field names.
FIELD_LABELS: dict[str, str] = {
    "shipper": "Shipper",
    "consignee": "Consignee",
    "notify_party": "Notify Party",
    "port_of_loading": "Port of Loading (POL)",
    "port_of_discharge": "Port of Discharge (POD)",
    "container_count": "Container Count",
    "gross_weight_kg": "Gross Weight (kg)",
}

REASON_LABELS: dict[str, str] = {
    "wrong_doc_type": "Attachment is not a draft BL (invoice / packing list)",
    "missing_attachment": "Expected attachment missing",
    "unreadable": "No readable text layer (likely an image-only scan)",
    "missing_value": "Blank or placeholder fields — not enough evidence to decide",
}


# ---------------------------------------------------------------------------
# Agent 决策轨迹（Pitch 用的「思考日志」，但每个数字都是实测的）
# ---------------------------------------------------------------------------
# `agent` 用机器 id（与真实编排器的 Agent.name 逐字一致），展示名交给前端按 role 映射。
# 两处命名不一致会让前端写两个渲染器 —— 这正是这类"看起来能用"的集成最容易翻车的地方。
AGENT_STEPS: tuple[tuple[str, str], ...] = (
    ("triage", "triage"),
    ("extractor", "extractor"),
    ("cross_verifier", "verifier"),
    ("escalation_judge", "judge"),
)


def _ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _agent_step(agent: str, role: str, duration_ms: float, summary: str, *,
                used_llm: bool = False, model: str | None = None,
                evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """构造一条轨迹步骤 —— 字段与 `agents.base.AgentStep.as_dict()` 逐键对齐。

    为什么在**导出时**记录，而不是让前端自己编故事：
      · `duration_ms` 是本次导出对该阶段原地计时的真实值；
      · `evidence` 全部取自真实产物（命中的规则名、解析到的字段数、相似度、缺陷集合）；
      · 因此面板上看到的与旁边那个红框来自**同一次计算**，不可能互相矛盾。
    上传链路（`/api/verify`）走的是真正的多 Agent 编排器，它落库的 payload.agents
    用的是同一个结构，前端一个渲染器就能吃两种来源。
    """
    base = {"agent": agent, "role": role, "duration_ms": duration_ms,
            "summary": summary, "used_llm": used_llm, "model": model,
            "error": None, "evidence": dict(evidence or {})}
    # 字段名漂移是静默 bug 的重灾区（曾把 row.field 写成 row.field_name），钉住它。
    from .agents.base import AgentStep
    assert set(base) == set(AgentStep(agent="", role="").as_dict()), base.keys()
    return base


def _agent_trace(
    *,
    email: Any,
    classification: Any,
    record: Mapping[str, Any],
    si_view: Any,
    bl_view: Any,
    field_rows: Sequence[Mapping[str, Any]],
    timings_ms: Mapping[str, float],
) -> tuple[list[dict[str, Any]], list[str]]:
    """把一封邮件的处理过程记成 4 条 Agent 步骤，返回 (trace, 被短路的 Agent)。"""
    category = record.get("category", "GENERAL")
    skipped: list[str] = []

    # ---- 1. Triage -------------------------------------------------------
    if classification is not None:
        triage_summary = (f"Expert rule matched '{classification.rule_name}' → {category}"
                          f" (confidence {classification.confidence:.2f})")
    else:
        triage_summary = f"No expert rule matched → model routing fallback → {category}"
    short_circuit = category != "BL_COMPARISON"
    if short_circuit:
        skipped = [name for name, _ in AGENT_STEPS[1:]]      # 机器 id，与编排器同口径
    trace = [_agent_step(
        "triage", "triage", timings_ms.get("triage", 0.0), triage_summary,
        used_llm=classification is None,
        evidence={
            "category": category,
            "decided_by": record.get("decided_by") or (classification.decided_by if classification else "rule"),
            "rule": classification.rule_name if classification else None,
            "confidence": round(classification.confidence, 3) if classification else None,
            "doc_issue_hint": classification.intent.doc_issue_hint if classification else None,
            "attachments": email.attachment_count,
            "short_circuited": short_circuit,
            "skipped_agents": skipped,
        })]
    if short_circuit:
        return trace, skipped

    # ---- 2. Extractor ----------------------------------------------------
    si_values = dict(si_view.values) if si_view is not None else {}
    bl_values = dict(bl_view.values) if bl_view is not None else {}
    si_filled = [name for name in COMPARE_FIELDS if si_values.get(name) not in (None, "")]
    bl_filled = [name for name in COMPARE_FIELDS if bl_values.get(name) not in (None, "")]
    mode = "PAIR" if si_view is not None and bl_view is not None else "SINGLE"
    # 通道名给人看：内部代号 `rule`/`llm` 直接展示会显得像缩写（judge 会问这是什么）
    extractor_names = ["multimodal" if name == "llm" else "expert-rules"
                       for name in {getattr(si_view, "extractor", None),
                                    getattr(bl_view, "extractor", None)} if name]
    trace.append(_agent_step(
        "extractor", "extractor", timings_ms.get("extractor", 0.0),
        f"{mode} extraction complete: SI {len(si_filled)}/7, BL {len(bl_filled)}/7 fields "
        f"(channel {'+'.join(sorted(extractor_names)) or 'none'})",
        evidence={
            "mode": mode,
            "channel": ("deterministic-label" if "expert-rules" in extractor_names
                        else "gemini-multimodal"),
            "llm_used": "multimodal" in extractor_names,
            "si_fields": si_filled, "bl_fields": bl_filled,
            "si_readable": bool(getattr(si_view, "is_readable", False)),
            "bl_readable": bool(getattr(bl_view, "is_readable", False)),
            "si_blank": sorted(si_view.blank_fields) if si_view is not None else [],
            "bl_blank": sorted(bl_view.blank_fields) if bl_view is not None else [],
        }))

    # ---- 3. Cross-Verifier ----------------------------------------------
    matched = [row["field"] for row in field_rows if row["verdict"] == "match"]
    defects = [row["field"] for row in field_rows if row["verdict"] == "defect"]
    undecided = [row["field"] for row in field_rows if row["verdict"] == "undecided"]
    methods: dict[str, int] = {}
    for row in field_rows:
        key = str(row.get("match_method"))
        methods[key] = methods.get(key, 0) + 1
    # evidence 的键名与真实编排器（agents/verifier.py）**逐键一致**：
    # 前端只写一个渲染器就能同时吃「导出快照」与「现算轨迹」两种来源。
    trace.append(_agent_step(
        "cross_verifier", "verifier", timings_ms.get("verifier", 0.0),
        f"Judgement matrix: {len(matched)} matched / {len(defects)} mismatched / "
        f"{len(undecided)} undecided (text ≥0.94, weight ±10kg or 0.2%)",
        evidence={
            "blank_is_defect": record.get("blank_is_defect", True),
            "matched_fields": matched, "defect_fields": defects,
            "undecided_fields": undecided,
            "match_methods": methods,
            "tolerances": {"text_similarity_threshold": 0.94,
                           "weight_abs_tolerance_kg": 10,
                           "weight_rel_tolerance_pct": 0.2},
            "similarities": {row["field"]: row.get("similarity") for row in field_rows
                             if row.get("similarity") is not None},
            "deltas": {row["field"]: row.get("delta") for row in field_rows
                       if row.get("delta") is not None},
        }))

    # ---- 4. Escalation Judge -------------------------------------------
    status = record.get("status", "OK")
    defect_fields = sorted(record.get("defect_fields") or [])
    reason = record.get("review_reason")
    # 措辞与 `agents/escalation_judge.py` 逐字对齐：快照轨迹与现算轨迹说的是同一句话，
    # 否则同一封邮件在两个入口下会用不同语气描述同一个结论（很容易被当成两套逻辑）。
    if status == "MISMATCH":
        judge_summary = (f"MISMATCH — {len(defect_fields)} field(s) form a compliance defect, "
                         f"reported directly")
    elif status == "NEEDS_REVIEW":
        judge_summary = (f"NEEDS_REVIEW — {REASON_LABELS.get(reason or '', reason)}")
    else:
        judge_summary = "OK — no defect fields, auto-filed with zero human touch"
    trace.append(_agent_step(
        "escalation_judge", "judge", timings_ms.get("judge", 0.0), judge_summary,
        evidence={
            "status": status, "defect_fields": defect_fields,
            "review_reason": reason,
            "review_reason_label": REASON_LABELS.get(reason or ""),
            "escalated": status == "NEEDS_REVIEW",
            "escalation_signals": dict(record.get("escalation_signals") or {}),
        }))
    return trace, skipped


def _field_rows(si_view: Any, bl_view: Any,
                report: Any | None = None) -> list[dict[str, Any]]:
    """7 个字段的并排明细（前端红框高亮的唯一数据源）。

    `report` 可传入**已经算好的** ComparisonReport 复用。这一点在「上传即核对」里很关键：
    上传链路已经用特定参数（blank_is_defect 由正文证据决定）算过一次比对，
    如果这里再默认重算一遍，展示的红框就会与最终裁决不是同一次计算 ——
    演示时会看到"界面标红但结论 OK"这种致命不一致。
    """
    if si_view is None or bl_view is None:
        return []
    if report is None:
        report = compare_documents(si_view, bl_view)
    by_field = report.by_field()
    rows: list[dict[str, Any]] = []
    for name in COMPARE_FIELDS:
        row = by_field.get(name)
        if row is None:
            continue
        rows.append({
            "field": name,
            "label": FIELD_LABELS.get(name, name),
            "si_raw": row.si_raw,
            "bl_raw": row.bl_raw,
            "si_normalized": row.si_normalized,
            "bl_normalized": row.bl_normalized,
            "is_match": bool(row.is_match),
            "match_method": row.match_method,
            "similarity": row.similarity,
            "delta": None if row.delta is None else float(row.delta),
            "needs_human": bool(row.needs_human),
            "needs_human_reason": row.needs_human_reason,
            "rationale": row.discrepancy_detail.get("rationale"),
            "detail": row.discrepancy_detail,
            # 三者决定前端怎么画：红框（缺陷）/ 黄框（待判）/ 绿框（一致）
            "verdict": ("defect" if (not row.is_match and not row.needs_human)
                        else "undecided" if row.needs_human else "match"),
        })
    return rows


def _slot_view(view: Any) -> dict[str, Any] | None:
    """把一个 DocumentView 压成前端要的最小摘要（不含全文，避免快照膨胀）。"""
    if view is None:
        return None
    return {
        "path": view.source_path,
        "file": (view.source_path or "").rsplit("/", 1)[-1],
        "readable": view.is_readable,
        "read_error": view.read_error,
        "doc_role": view.doc_role,
        "other_doc_kind": view.other_doc_kind,
        "blank_fields": sorted(view.blank_fields),
        "values": {name: view.values.get(name) for name in COMPARE_FIELDS},
    }


def build_dashboard(
    submission: Mapping[str, Mapping[str, Any]],
    *,
    data_dir: str = "data",
    include_body_chars: int = 1200,
) -> dict[str, Any]:
    """把 submission + 附件明细合成一份自包含快照。"""
    source = InboxSource("data", data_dir=resolve_data_dir(data_dir))
    emails = source.emails()

    # 边导出边计时：这段循环跑的就是**真实的确定性链路**
    # （读附件 → 标签抽取 → 判定矩阵），所以量出来的秒数可以直接当机器耗时用，
    # 不需要另跑一遍跑批。ROI 卡上的"Time Saved"因此是同一次导出的实测值。
    machine_started = time.perf_counter()
    records: list[dict[str, Any]] = []
    for email in emails:
        record = dict(submission.get(email.email_id) or {})
        triage_started = time.perf_counter()
        classification = rule_classify(email)
        triage_ms = _ms_since(triage_started)
        timings: dict[str, float] = {"triage": triage_ms,
                                     "extractor": 0.0, "verifier": 0.0, "judge": 0.0}
        entry: dict[str, Any] = {
            "email_id": email.email_id,
            "from": email.sender,
            "subject": email.subject,
            "body": (email.body or "")[:include_body_chars],
            "attachments": [path.rsplit("/", 1)[-1] for path in email.attachments],
            "attachment_count": email.attachment_count,
            "category": record.get("category", "GENERAL"),
            "status": record.get("status", "OK"),
            "has_defect": bool(record.get("has_defect")),
            "defect_fields": sorted(record.get("defect_fields") or []),
            "review_reason": record.get("review_reason"),
            "review_reason_label": REASON_LABELS.get(record.get("review_reason") or "", None),
            "decided_by": record.get("decided_by")
            or (classification.decided_by if classification else "rule"),
            "rule_name": classification.rule_name if classification else None,
            "category_confidence": round(classification.confidence, 3) if classification else None,
            "body_hint": classification.intent.doc_issue_hint if classification else None,
            "si": None,
            "bl": None,
            "fields": [],
        }

        # 只有真正可比的邮件才去读附件（省 IO，也让快照更小）
        si_view = bl_view = None
        field_rows: list[dict[str, Any]] = []
        if record.get("category") == "BL_COMPARISON" and email.attachment_count > 0:
            read_started = time.perf_counter()
            si_path, bl_path = split_attachments(email.attachments)
            # 每份附件只读一次：同一份 DocumentText 既出展示摘要，又进判定矩阵
            si_slot = DocSlot(ROLE_SI, read_document(source, si_path)) if si_path else None
            bl_slot = DocSlot(ROLE_BL, read_document(source, bl_path)) if bl_path else None
            si_view = extract_document_view("SI", si_slot.document) if si_slot else None
            bl_view = extract_document_view("BL", bl_slot.document) if bl_slot else None
            timings["extractor"] = _ms_since(read_started)
            compare_started = time.perf_counter()
            field_rows = _field_rows(si_view, bl_view)
            timings["verifier"] = _ms_since(compare_started)
            entry["si"] = _slot_view(si_view)
            entry["bl"] = _slot_view(bl_view)
            entry["fields"] = field_rows

        # Agent 轨迹：与上面的红框、以及 submission 里的结论同源
        judge_started = time.perf_counter()
        trace, skipped = _agent_trace(
            email=email, classification=classification, record=record,
            si_view=si_view, bl_view=bl_view, field_rows=field_rows, timings_ms=timings)
        timings["judge"] = _ms_since(judge_started)
        # judge 的时间就是上面这次装配耗时；补进 trace 里（保真实，不凑数）
        for step in trace:
            if step["role"] == "judge":
                step["duration_ms"] = timings["judge"]
        entry["trace"] = trace
        entry["trace_skipped_agents"] = skipped
        entry["trace_total_ms"] = round(sum(step["duration_ms"] for step in trace), 3)
        records.append(entry)

    machine_seconds = time.perf_counter() - machine_started
    records.sort(key=lambda item: (item["status"] != "MISMATCH",
                                   item["status"] != "NEEDS_REVIEW",
                                   item["email_id"]))
    summary = summarize(records)
    summary["machine_seconds"] = round(machine_seconds, 3)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        # GBS ROI：三张大卡的唯一数据源（假设与推导明细一并带上，供 tooltip 展示）
        "roi": compute_roi(records, machine_seconds=machine_seconds).as_dict(),
        "field_labels": FIELD_LABELS,
        "reason_labels": REASON_LABELS,
        "emails": records,
    }


def summarize(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def count(predicate) -> int:
        return sum(1 for record in records if predicate(record))

    categories: dict[str, int] = {}
    statuses: dict[str, int] = {}
    reasons: dict[str, int] = {}
    defects: dict[str, int] = {}
    for record in records:
        categories[record["category"]] = categories.get(record["category"], 0) + 1
        statuses[record["status"]] = statuses.get(record["status"], 0) + 1
        reason = record.get("review_reason")
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
        for field in record.get("defect_fields") or []:
            defects[field] = defects.get(field, 0) + 1
    total = len(records)
    return {
        "total": total,
        "categories": dict(sorted(categories.items(), key=lambda kv: -kv[1])),
        "statuses": dict(sorted(statuses.items(), key=lambda kv: -kv[1])),
        "review_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "defect_fields": dict(sorted(defects.items(), key=lambda kv: -kv[1])),
        "mismatches": count(lambda r: r["status"] == "MISMATCH"),
        "needs_review": count(lambda r: r["status"] == "NEEDS_REVIEW"),
        "bl_comparison": count(lambda r: r["category"] == "BL_COMPARISON"),
        "rule_decided": count(lambda r: r["decided_by"] == "rule"),
        "rule_pct": round(100.0 * count(lambda r: r["decided_by"] == "rule") / max(total, 1), 2),
        "with_attachments": count(lambda r: r["attachment_count"] > 0),
    }


def write_dashboard(payload: Mapping[str, Any], path: Path | None = None) -> Path:
    target = Path(path or DEFAULT_DASHBOARD_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def load_dashboard(path: Path | None = None) -> dict[str, Any] | None:
    target = Path(path or DEFAULT_DASHBOARD_PATH)
    if not target.is_file():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


if __name__ == "__main__":  # pragma: no cover
    import sys

    from .console import harden_console

    harden_console()
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if source:
        submission = json.loads(Path(source).read_text(encoding="utf-8"))
    else:
        from .config import DEFAULT_SUBMISSION_PATH
        submission = json.loads(DEFAULT_SUBMISSION_PATH.read_text(encoding="utf-8"))
    payload = build_dashboard(submission)
    print("看板已写出：", write_dashboard(payload))
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
