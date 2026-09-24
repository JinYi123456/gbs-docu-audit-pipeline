"""全量编排批处理引擎。

一封邮件：分类 → 读附件 → 提取 → 归一+比对 → 升级裁决 → 组装 submission

设计要点：
  · 落库与产物解耦 —— Supabase 没配也能跑出 submission.json，先本地空跑再开落库
  · 单封失败绝不中断整批（每封 try/except，失败记入 failures 并保留安全默认值）
  · 确定性路径与 LLM 路径共用同一套裁决逻辑，保证"有无 API Key 结果同源"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from .config import (
    DEFAULT_SUBMISSION_PATH, RuntimeSettings, gemini_api_key_available, resolve_data_dir,
)
from .console import harden_console
from .ingest.inbox import InboxEmail, InboxSource
from .pipeline.classify import (
    CATEGORY_BL_COMPARISON, CATEGORY_GENERAL, ClassificationResult, classify_email,
    rule_classify,
)
from .pipeline.compare import (
    REASON_MISSING_VALUE, REASON_UNREADABLE, ComparisonReport, compare_documents,
)
from .pipeline.extract import ExtractionResult, extract_document_pair, load_slots
from .pipeline.policy import EscalationContext, PolicyOutcome, apply_policy
from .pipeline.rule_extract import coverage_report

logger = logging.getLogger("sdoc.runner")

EMPTY_RECORD: Final[dict[str, Any]] = {
    "category": CATEGORY_GENERAL, "status": "OK", "review_reason": None,
    "has_defect": False, "defect_fields": [],
}


@dataclass(slots=True)
class RunConfig:
    source: str = "data"
    data_dir: str = "data"
    limit: int | None = None
    concurrency: int = 6
    use_rules: bool = True
    use_llm: bool = True
    write_db: bool = False
    submission_out: Path = DEFAULT_SUBMISSION_PATH
    report_dir: Path | None = None
    tag: str = ""
    quiet: bool = False


@dataclass(slots=True)
class RunSummary:
    started_at: str = ""
    finished_at: str = ""
    emails: int = 0
    classified: int = 0
    rule_decided: int = 0
    llm_decided: int = 0
    category_counts: dict[str, int] = field(default_factory=dict)
    bl_comparison: int = 0
    compared: int = 0
    mismatches: int = 0
    needs_review: int = 0
    defects_caught: dict[str, int] = field(default_factory=dict)
    review_reasons: dict[str, int] = field(default_factory=dict)
    extraction_coverage: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    llm: dict[str, Any] = field(default_factory=dict)
    write: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at, "finished_at": self.finished_at,
            "emails": self.emails, "classified": self.classified,
            "rule_decided": self.rule_decided, "llm_decided": self.llm_decided,
            "category_counts": self.category_counts,
            "bl_comparison": self.bl_comparison, "compared": self.compared,
            "mismatches": self.mismatches, "needs_review": self.needs_review,
            "defects_caught": self.defects_caught,
            "review_reasons": self.review_reasons,
            "extraction_coverage": self.extraction_coverage,
            "failures": self.failures[:50], "llm": self.llm, "write": self.write,
            "duration_s": round(self.duration_s, 2),
        }


# ---------------------------------------------------------------------------
# 裁决：分类 + 附件形态 + 文档级比对结论 → 官方 5 键
# ---------------------------------------------------------------------------
def _placeholder_report(extraction: ExtractionResult) -> ComparisonReport:
    """缺一侧附件时的占位 report（policy 只看 review_reason 与 readable 信号）。"""
    si_readable = bool(extraction.si is not None and extraction.si.is_readable)
    bl_readable = bool(extraction.bl is not None and extraction.bl.is_readable)
    return ComparisonReport(
        comparisons=(), defect_fields=(), matched_fields=(), undecided_fields=(),
        status="OK", has_defect=False,
        review_reason=None if (si_readable or bl_readable) else REASON_UNREADABLE,
        escalation_signals={"si_readable": si_readable, "bl_readable": bl_readable},
    )


def decide(
    email: InboxEmail,
    classification: ClassificationResult,
    extraction: ExtractionResult | None,
) -> tuple[dict[str, Any], PolicyOutcome, ComparisonReport | None]:
    """把三份证据合成最终裁决。这是全系统唯一的结论出口。"""
    context = EscalationContext(
        attachment_count=email.attachment_count,
        si_present=bool(extraction and extraction.si is not None),
        bl_present=bool(extraction and extraction.bl is not None),
        body_asserts_documents_attached=classification.intent.asserts_documents_attached,
        body_hint=classification.intent.doc_issue_hint,
        min_field_confidence=_min_confidence(extraction),
    )

    if extraction is None or extraction.si is None or extraction.bl is None:
        report = None if extraction is None else _placeholder_report(extraction)
        outcome = apply_policy(report, context) if report is not None else apply_policy(
            _placeholder_report(ExtractionResult(email_id=email.email_id, mode="NONE")), context)
        return _record(classification.category, outcome), outcome, report

    # 单侧空白到底是「缺陷」还是「不确定」，由正文证据决定（见 compare.compare_field 文档）：
    # 正文声明了「客户留空」→ 单据本就不完整 → 空白只能算不确定（email_516–520）；
    # 否则一侧有值另一侧为空 = 值在传递中丢失 = 实质不一致（email_313/351）。
    blank_is_defect = classification.intent.doc_issue_hint != REASON_MISSING_VALUE
    report = compare_documents(extraction.si, extraction.bl, blank_is_defect=blank_is_defect)
    outcome = apply_policy(report, context)
    return _record(classification.category, outcome), outcome, report


def _min_confidence(extraction: ExtractionResult | None) -> float:
    if extraction is None:
        return 1.0
    values = [value for block in (extraction.si, extraction.bl) if block is not None
              for value in block.field_confidence.values()]
    return min(values) if values else 1.0


def _record(category: str, outcome: PolicyOutcome) -> dict[str, Any]:
    return {
        "category": category,
        "status": outcome.status,
        "review_reason": outcome.review_reason,
        "has_defect": bool(outcome.has_defect),
        "defect_fields": sorted(outcome.defect_fields),
    }


# ---------------------------------------------------------------------------
# 单封处理
# ---------------------------------------------------------------------------
def process_email_deterministic(
    source: InboxSource,
    email: InboxEmail,
) -> tuple[dict[str, Any], ClassificationResult]:
    """纯确定性路径（无网络、无 API Key）—— 回归测试与离线预览都走这里。"""
    classification = rule_classify(email)
    if classification is None:
        classification = ClassificationResult(
            email_id=email.email_id, category=CATEGORY_GENERAL, confidence=0.3,
            decided_by="rule", evidence_span="(no rule matched)", rule_name="fallback:general",
            error="llm_disabled")

    if classification.category != CATEGORY_BL_COMPARISON:
        return _record(classification.category,
                       PolicyOutcome(status="OK", has_defect=False, defect_fields=(),
                                     review_reason=None, needs_review_queue=False,
                                     rationale="not a comparison email")), classification

    if email.attachment_count == 0:
        # 0 附件：没有可比对对象，交给 policy 按正文证据裁决
        record, _, _ = decide(email, classification, None)
        return record, classification

    si_slot, bl_slot = load_slots(source, email.attachments)
    extraction = extract_document_pair_sync(email.email_id, si_slot, bl_slot)
    record, _, _ = decide(email, classification, extraction)
    return record, classification


def extract_document_pair_sync(
    email_id: str,
    si_slot: Any,
    bl_slot: Any,
) -> ExtractionResult:
    from .pipeline.extract import extract_rule
    return extract_rule(email_id, si_slot, bl_slot)


async def process_email(
    source: InboxSource,
    email: InboxEmail,
    client: Any | None,
    config: RunConfig,
) -> tuple[dict[str, Any], ClassificationResult, ExtractionResult | None, PolicyOutcome,
           ComparisonReport | None]:
    """完整路径（可带 LLM）。"""
    classification = await classify_email(email, client=client, use_rules=config.use_rules,
                                          use_llm=config.use_llm and client is not None)

    if classification.category != CATEGORY_BL_COMPARISON:
        outcome = PolicyOutcome(status="OK", has_defect=False, defect_fields=(),
                                review_reason=None, needs_review_queue=False,
                                rationale="not a comparison email; no comparison needed")
        return _record(classification.category, outcome), classification, None, outcome, None

    if email.attachment_count == 0:
        record, outcome, report = decide(email, classification, None)
        return record, classification, None, outcome, report

    si_slot, bl_slot = load_slots(source, email.attachments)
    extraction = await extract_document_pair(
        email.email_id, si_slot, bl_slot, client=client,
        use_llm=config.use_llm and client is not None)
    record, outcome, report = decide(email, classification, extraction)
    return record, classification, extraction, outcome, report


# ---------------------------------------------------------------------------
# 全量编排
# ---------------------------------------------------------------------------
async def run_pipeline(config: RunConfig) -> tuple[dict[str, Any], RunSummary]:
    summary = RunSummary(started_at=datetime.now(timezone.utc).isoformat())
    started = time.perf_counter()

    source = InboxSource(config.source, data_dir=resolve_data_dir(config.data_dir))
    emails = source.emails()
    if config.limit:
        emails = emails[:config.limit]
    summary.emails = len(emails)
    logger.info("载入 %d 封邮件（source=%s）", len(emails), config.source)

    client = None
    if config.use_llm:
        if gemini_api_key_available():
            from .llm.gemini import GeminiSettings, get_client
            try:
                client = get_client(GeminiSettings.from_env())
            except Exception as exc:      # noqa: BLE001
                logger.error("Gemini 客户端初始化失败：%s", exc)
        else:
            logger.warning("未检测到 GEMINI_API_KEY —— 自动走规则通道（离线可跑通）")

    submission: dict[str, Any] = {email.email_id: dict(EMPTY_RECORD) for email in emails}
    units: list[Any] = []          # 有比对证据的邮件（抽取/比对行）
    processed: list[Any] = []      # 本轮**全部**邮件（父行必须有，视图才是 520 键）
    semaphore = asyncio.Semaphore(max(1, config.concurrency))
    lock = asyncio.Lock()

    async def worker(index: int, email: InboxEmail) -> None:
        async with semaphore:
            try:
                record, classification, extraction, outcome, report = await process_email(
                    source, email, client, config)
            except Exception as exc:      # noqa: BLE001 —— 单封失败绝不能中断整批
                async with lock:
                    summary.failures.append(
                        f"{email.email_id}: {type(exc).__name__}: {exc}"[:200])
                    # 失败的邮件也要留下落库痕迹（走 submission 的安全默认记录），
                    # 否则云端少一行 → 视图缺键 → 提交产物比 520 少，官方按缺失记罚。
                    processed.append((email, None, None, None, None))
                return
            async with lock:
                submission[email.email_id] = record
                _accumulate(summary, email, classification, extraction, outcome, report)
                # 有比对证据 → 登记为 unit（写抽取/比对子行）
                if report is not None and email.attachment_count:
                    units.append((email, classification, extraction, outcome, report))
                # 所有邮件都要留下落库痕迹（含 0 附件的索要类与垃圾邮件）——
                # 必须在 if 之外：父行缺失会让云端视图少于 520 键。
                # （早先写成了嵌套 if，processed 仍是 126 条，症状就是视图缺键）
                processed.append((email, classification, extraction, outcome, report))

    await asyncio.gather(*(worker(index, email) for index, email in enumerate(emails, start=1)))

    if config.write_db and processed:
        # ★ 落库必须覆盖**全部**邮件，而不只是有比对行的那批。
        #   实测踩过：早先只传 `units`（=有附件的对照类，126 封）上云，
        #   submission_view 就只有 126 行 —— 而官方提交要恰好 520 个键，
        #   缺的 394 个被当成 GENERAL 记罚，云端口径直接报废。
        #   现在：所有邮件都写父行（裁决取权威 submission 记录），
        #   抽取/比对行仅在有证据时写；没比对行的还会被 prune 清掉旧数据，
        #   于是"重跑 = 幂等"与"视图 = 520 键"同时成立。
        summary.write = await _write_to_db(
            source, processed, submission, [email.email_id for email in emails])

    _dump_outputs(config, submission, summary, client)
    summary.finished_at = datetime.now(timezone.utc).isoformat()
    summary.duration_s = time.perf_counter() - started
    return dict(sorted(submission.items())), summary


def _accumulate(
    summary: RunSummary,
    email: InboxEmail,
    classification: ClassificationResult,
    extraction: ExtractionResult | None,
    outcome: PolicyOutcome,
    report: ComparisonReport | None,
) -> None:
    summary.classified += 1
    summary.category_counts[classification.category] = \
        summary.category_counts.get(classification.category, 0) + 1
    if classification.decided_by == "rule":
        summary.rule_decided += 1
    else:
        summary.llm_decided += 1

    if classification.category != CATEGORY_BL_COMPARISON:
        return
    summary.bl_comparison += 1
    if outcome.status in {"OK", "MISMATCH"}:
        summary.compared += 1
    if outcome.status == "MISMATCH":
        summary.mismatches += 1
        for name in outcome.defect_fields:
            summary.defects_caught[name] = summary.defects_caught.get(name, 0) + 1
    if outcome.status == "NEEDS_REVIEW":
        summary.needs_review += 1
        reason = outcome.review_reason or "unknown"
        summary.review_reasons[reason] = summary.review_reasons.get(reason, 0) + 1

    for view in ((extraction.si if extraction else None),
                 (extraction.bl if extraction else None)):
        if view is None:
            continue
        report_row = coverage_report(view)
        summary.extraction_coverage["readable" if view.is_readable else "unreadable"] = \
            summary.extraction_coverage.get("readable" if view.is_readable else "unreadable", 0) + 1
        for missing in report_row["missing"]:
            key = f"missing:{missing}"
            summary.extraction_coverage[key] = summary.extraction_coverage.get(key, 0) + 1


async def _write_to_db(
    source: InboxSource,
    processed: Sequence[Any],
    submission: Mapping[str, Any],
    all_email_ids: Sequence[str],
) -> dict[str, Any]:
    """落库（需要 Supabase 环境变量）。幂等 upsert，重跑无副作用。

    ★ 裁决字段**以 `submission` 为唯一真相**，而不是从 outcome 重算。
      理由：submission 是即将提交给官方的产物（已过官方 5 键合约校验），
      落库口径必须与它逐字一致，否则会出现"库里说 MISMATCH、提交产物说 OK"
      这种最难以排查的偏差。非对照类邮件在 submission 里本就是干净的 OK。
    """
    try:
        from .db.repo import (
            EmailWrite, ExtractionWrite, PipelineUnit, upsert_verification_pipeline_results,
        )
    except ImportError as exc:
        logger.error("落库模块不可用（缺 supabase SDK？）：%s", exc)
        return {"error": f"repo unavailable: {exc}"}

    payloads: list[Any] = []
    for email, classification, extraction, outcome, report in processed:
        record = submission.get(email.email_id) or {}
        verdict = record.get("status") or "OK"
        category = record.get("category") or "GENERAL"
        writes = []
        for slot in (extraction.si if extraction else None,
                     extraction.bl if extraction else None):
            if slot is None:
                continue
            writes.append(ExtractionWrite(
                email_id=email.email_id, doc_type=slot.doc_type,
                source_path=slot.source_path or "", values=dict(slot.values),
                field_confidence=dict(slot.field_confidence),
                field_blank=sorted(slot.blank_fields),
                doc_role_detected=slot.doc_role, other_doc_kind=slot.other_doc_kind,
                extractor_model=extraction.model if extraction else "rule",
                prompt_version=extraction.prompt_version if extraction else "",
                is_readable=slot.is_readable, read_error=slot.read_error))

        state = "RECEIVED"
        if classification is not None:
            state = "CLASSIFIED"
        if report is not None:
            state = "ESCALATED" if verdict == "NEEDS_REVIEW" else "COMPARED"

        payloads.append(PipelineUnit(
            email=EmailWrite(
                email_id=email.email_id, subject=email.subject, body=email.body,
                from_addr=email.sender, attachment_paths=list(email.attachments),
                category=category,
                category_confidence=classification.confidence if classification else 0.0,
                classified_by=classification.decided_by if classification else "rule",
                body_hint=(classification.intent.doc_issue_hint if classification else None),
                verdict_status=verdict,
                has_defect=bool(record.get("has_defect")),
                defect_fields=sorted(record.get("defect_fields") or []),
                review_reason=record.get("review_reason"),
                pipeline_state=state,
                prompt_version=classification.prompt_version if classification else "",
                model_classify=classification.model if classification else None,
                latency_ms=classification.latency_ms if classification else None,
                processed_at=datetime.now(timezone.utc).isoformat()),
            extractions=tuple(writes),
            comparisons=tuple(report.to_db_rows(email.email_id)) if report else ()))

    repo_report = await upsert_verification_pipeline_results(
        payloads, prune_email_ids=list(all_email_ids))
    return repo_report.as_dict()


def _dump_outputs(
    config: RunConfig,
    submission: dict[str, Any],
    summary: RunSummary,
    client: Any | None,
) -> None:
    from .contracts import validate_record
    problems: list[str] = []
    for email_id, record in submission.items():
        for problem in validate_record(record):
            problems.append(f"{email_id}: {problem}")
            if len(problems) >= 10:
                break
        if len(problems) >= 10:
            break
    if problems:
        raise ValueError("submission violates the official 5-key contract: " + "; ".join(problems[:5]))

    config.submission_out.parent.mkdir(parents=True, exist_ok=True)
    config.submission_out.write_text(
        json.dumps(dict(sorted(submission.items())), indent=2, ensure_ascii=False),
        encoding="utf-8")

    summary.llm = client.usage.as_dict() if client is not None else {"mode": "rule-only"}
    if config.report_dir:
        config.report_dir.mkdir(parents=True, exist_ok=True)
        (config.report_dir / "summary.json").write_text(
            json.dumps(summary.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SDOC 跑批引擎：520 封邮件 → 官方 5 键 submission.json")
    settings = RuntimeSettings.from_env()
    parser.add_argument("--source", default=settings.inbox_source,
                        help="data 或 http://localhost:8080")
    parser.add_argument("--data-dir", default=settings.data_dir)
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 封（冒烟）")
    parser.add_argument("--concurrency", type=int, default=settings.concurrency)
    parser.add_argument("--no-llm", action="store_true",
                        help="强制走规则通道（无 API Key 也能全流程跑通）")
    parser.add_argument("--no-rules", action="store_true", help="跳过规则层，全部交给 LLM")
    parser.add_argument("--write-db", action="store_true", help="写入 Supabase 三张表")
    parser.add_argument("--out", type=Path, default=DEFAULT_SUBMISSION_PATH)
    parser.add_argument("--report-dir", type=Path, default=None)
    parser.add_argument("--tag", default="")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    harden_console()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    config = RunConfig(
        source=args.source, data_dir=args.data_dir, limit=args.limit,
        concurrency=args.concurrency, use_rules=not args.no_rules,
        use_llm=not args.no_llm, write_db=args.write_db,
        submission_out=args.out, report_dir=args.report_dir, tag=args.tag,
        quiet=args.quiet)
    submission, summary = asyncio.run(run_pipeline(config))

    print()
    print("=" * 74)
    print(f"跑批完成：{summary.emails} 封，用时 {summary.duration_s:.1f}s")
    print(f"  分类分布      : {summary.category_counts}")
    print(f"  规则/LLM 决策  : {summary.rule_decided} / {summary.llm_decided}")
    print(f"  BL_COMPARISON : {summary.bl_comparison}"
          f"（可比对 {summary.compared}，缺陷 {summary.mismatches}，人审 {summary.needs_review}）")
    print(f"  命中的缺陷字段 : {summary.defects_caught}")
    print(f"  人审理由分布   : {summary.review_reasons}")
    if summary.failures:
        print(f"  失败           : {len(summary.failures)} 封（前 3：{summary.failures[:3]}）")
    print(f"  产物           : {args.out}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
