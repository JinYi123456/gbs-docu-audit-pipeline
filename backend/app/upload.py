"""「上传即核对」引擎 —— 用户拖进来两份文件，立刻拿到逐字段红框结论。

设计要点（每条都是刻意的，不是顺手写的）：

1. **复用同一条链路，绝不另写一套"演示专用"逻辑**。
   上传走的是 classify → extract → compare → policy 这条与 520 全量跑批**完全同源**
   的链路。展示与评分同源，才不会出现"演示时对、提交时错"的经典翻车。

2. **上传件绝不写进 emails 表**。
   官方终局产物是恰好 520 个 email_id 的 5 键集合，多一个未知键即扣分；
   而 submission_view 是从 emails 聚合的。所以上传只落 `upload_runs`（迁移 0005），
   与评测数据物理隔离 —— 现场随便点上传也不会污染提交产物。

3. **双通道并存**：给了 Gemini 客户端就走多模态原生 PDF 抽取（主推），
   没有 Key 也能走确定性规则通道，界面照常出结果。演示前断网也不慌。

4. **任何一侧缺失/不可读都不抛异常**，而是走 policy 的升级阶梯给出
   NEEDS_REVIEW + 理由（missing_attachment / unreadable / wrong_doc_type）——
   与全量跑批的行为完全一致。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import DEFAULT_REPORT_DIR, resolve_data_dir
from .dashboard import FIELD_LABELS, REASON_LABELS, _field_rows, _slot_view
from .ingest.inbox import InboxEmail
from .ingest.readers import DocumentText, read_document
from .pipeline.classify import (
    CATEGORY_BL_COMPARISON, CATEGORY_GENERAL, ClassificationResult, IntentFlagsResult,
    attachment_expectation, intent_flags, rule_classify, strip_boilerplate,
)
from .pipeline.compare import (
    REASON_MISSING_VALUE, REASON_UNREADABLE, ComparisonReport, compare_documents,
)
from .pipeline.extract import ROLE_BL, ROLE_SI, DocSlot, ExtractionResult, extract_document_pair
from .pipeline.policy import EscalationContext, PolicyOutcome, apply_policy

logger = logging.getLogger("sdoc.upload")

DEFAULT_UPLOAD_DIR: Path = DEFAULT_REPORT_DIR / "uploads"
MAX_FILE_BYTES: int = 40 * 1024 * 1024          # 单文件 40MB 上限（PDF 单据远小于此）
MAX_FILES: int = 4
SUPPORTED_SUFFIXES: frozenset[str] = frozenset(
    {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".csv", ".png", ".jpg", ".jpeg"})


class UploadError(ValueError):
    """输入层面的错误（文件太多、太大、类型不支持）—— API 层应转 422。"""


@dataclass(slots=True, frozen=True)
class UploadedFile:
    """一份上传件（内存中，不落盘直到核对成功）。"""

    name: str
    data: bytes
    role: str = ""            # 表单显式声明的角色："SI" / "BL" / ""（交给推断）

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def suffix(self) -> str:
        return ("." + self.name.rsplit(".", 1)[-1].lower()) if "." in self.name else ""


@dataclass(slots=True)
class UploadOutcome:
    record: dict[str, Any]
    run_id: str
    duration_ms: int
    storage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"record": self.record, "run_id": self.run_id,
                "duration_ms": self.duration_ms, "storage": self.storage}


# ---------------------------------------------------------------------------
# 内存数据源：让 read_document / extract 直接吃上传的字节
# ---------------------------------------------------------------------------
class MemorySource:
    """与 InboxSource 的接口子集（read_document 只用到 read_bytes）。"""

    def __init__(self, files: Sequence[UploadedFile]) -> None:
        self._by_path = {_safe_name(upload.name): upload.data for upload in files}
        self._by_basename = {Path(_safe_name(upload.name)).name: upload.data for upload in files}

    def read_bytes(self, attachment_path: str) -> bytes:
        if attachment_path in self._by_path:
            return self._by_path[attachment_path]
        basename = Path(attachment_path).name
        if basename in self._by_basename:
            return self._by_basename[basename]
        raise FileNotFoundError(f"上传件不存在：{attachment_path}")


def _safe_name(name: str) -> str:
    """只保留文件名部分，剥掉任何路径成分（`../evil.pdf` 之类不能穿透）。"""
    cleaned = Path(str(name).replace("\\", "/")).name.strip()
    return cleaned or "upload.bin"


def validate_files(files: Sequence[UploadedFile]) -> None:
    if not files:
        raise UploadError("Attach at least one document (SI or draft BL).")
    if len(files) > MAX_FILES:
        raise UploadError(f"At most {MAX_FILES} documents per audit; received {len(files)}.")
    for upload in files:
        if not upload.data:
            raise UploadError(f"{upload.name} is an empty file (0 bytes).")
        if upload.size > MAX_FILE_BYTES:
            raise UploadError(
                f"{upload.name} exceeds the {MAX_FILE_BYTES // 1024 // 1024}MB limit.")
        if upload.suffix and upload.suffix not in SUPPORTED_SUFFIXES:
            raise UploadError(
                f"Unsupported file type '{upload.suffix}' for {upload.name}; "
                f"supported: {', '.join(sorted(SUPPORTED_SUFFIXES))}")


def assign_roles(files: Sequence[UploadedFile]) -> tuple[UploadedFile | None, UploadedFile | None]:
    """决定哪份是 SI、哪份是 BL。优先级：

    1. 表单显式声明的 role（最可信：用户点的是「上传 SI」按钮）
    2. 文件名后缀 `_SI` / `_BL`（与官方语料命名约定一致）
    3. 依次兜底：第一份 SI、第二份 BL（**可预测**优于聪明）
    """
    by_role: dict[str, UploadedFile] = {}
    for upload in files:
        role = (upload.role or "").strip().upper()
        if role in {"SI", "BL"} and role not in by_role:
            by_role[role] = upload

    leftovers = [upload for upload in files if upload not in by_role.values()]
    for upload in list(leftovers):
        stem = Path(_safe_name(upload.name)).stem.upper()
        if stem.endswith("_SI") and "SI" not in by_role:
            by_role["SI"] = upload
            leftovers.remove(upload)
        elif stem.endswith("_BL") and "BL" not in by_role:
            by_role["BL"] = upload
            leftovers.remove(upload)

    if "SI" not in by_role and leftovers:
        by_role["SI"] = leftovers.pop(0)
    if "BL" not in by_role and leftovers:
        by_role["BL"] = leftovers.pop(0)
    return by_role.get("SI"), by_role.get("BL")


# ---------------------------------------------------------------------------
# 主链路
# ---------------------------------------------------------------------------
def _placeholder_report(extraction: ExtractionResult) -> ComparisonReport:
    """缺一侧时的占位 report（policy 只看 review_reason 与 readable 信号）。"""
    si_readable = bool(extraction.si is not None and extraction.si.is_readable)
    bl_readable = bool(extraction.bl is not None and extraction.bl.is_readable)
    return ComparisonReport(
        comparisons=(), defect_fields=(), matched_fields=(), undecided_fields=(),
        status="OK", has_defect=False,
        review_reason=None if (si_readable or bl_readable) else REASON_UNREADABLE,
        escalation_signals={"si_readable": si_readable, "bl_readable": bl_readable},
    )


def _classify_upload(email: InboxEmail, *, client: Any | None, use_llm: bool) -> ClassificationResult:
    ruled = rule_classify(email)
    if ruled is not None:
        return ruled
    flags = intent_flags(strip_boilerplate(email.body))
    return ClassificationResult(
        email_id=email.email_id, category=CATEGORY_GENERAL, confidence=0.4,
        decided_by="rule", evidence_span="(on-demand upload: no rule matched)",
        attachment_expectation=attachment_expectation(email), intent=flags,
        rule_name="upload:fallback")


async def verify_uploaded_documents(
    files: Sequence[UploadedFile],
    *,
    subject: str = "",
    body: str = "",
    sender: str = "uploader@local",
    use_llm: bool = False,
    client: Any | None = None,
    run_id: str | None = None,
    persist_local: bool = True,
    persist_cloud: bool = False,
    upload_dir: Path | None = None,
) -> dict[str, Any]:
    """跑完一条完整链路，返回与 dashboard 快照里 `emails[i]` **同构**的记录。

    同构是刻意的：前端可以直接把结果丢进现成的 `FieldDiff` 组件渲染，
    不需要为上传场景再写一套展示逻辑。
    """
    started = time.perf_counter()
    validate_files(files)
    si_upload, bl_upload = assign_roles(files)
    document_names = tuple(_safe_name(upload.name)
                           for upload in (si_upload, bl_upload) if upload is not None)
    resolved_run_id = run_id or f"upload_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_" \
                                f"{hashlib.sha1(b''.join(u.data for u in files)).hexdigest()[:8]}"

    email = InboxEmail(
        email_id=resolved_run_id, sender=sender,
        subject=subject or f"On-demand audit: {' + '.join(document_names)}",
        body=body or "", attachments=document_names, raw={})

    source = MemorySource(files)
    si_slot = DocSlot(ROLE_SI, read_document(source, _safe_name(si_upload.name))) if si_upload else None
    bl_slot = DocSlot(ROLE_BL, read_document(source, _safe_name(bl_upload.name))) if bl_upload else None

    # ★ 走**多 Agent 编排器**，而不是在这里手写一遍顺序调用。
    #   全量跑批与上传因此共用同一条链：Triage → Extractor → CrossVerifier → Judge。
    #   同源的价值在于"展示的就是评分的"：演示时看到红框，与 520 全量提交用的是
    #   同一套判定矩阵与升级阶梯，不存在"演示版逻辑"。
    from .agents.orchestrator import verify_email

    orchestration = await verify_email(
        email, source=None, client=client,
        use_llm=use_llm and client is not None,
        si_slot=si_slot, bl_slot=bl_slot, slots_provided=True)
    state = orchestration.state
    classification = state.classification if state.classification is not None else \
        await _classify_upload_async(email, client=client, use_llm=use_llm)
    extraction: ExtractionResult | None = state.extraction
    report = state.report or _placeholder_report(
        extraction or ExtractionResult(email_id=email.email_id, mode="NONE"))
    outcome: PolicyOutcome = state.outcome or apply_policy(
        report, EscalationContext(attachment_count=email.attachment_count,
                                  si_present=si_slot is not None,
                                  bl_present=bl_slot is not None))
    si_view, bl_view = state.si_view, state.bl_view

    record = _build_record(
        email=email, classification=classification, extraction=extraction,
        outcome=outcome, report=report, si_view=si_view, bl_view=bl_view,
        si_upload=si_upload, bl_upload=bl_upload, run_id=resolved_run_id)
    duration_ms = int((time.perf_counter() - started) * 1000)
    record["duration_ms"] = duration_ms
    # 多 Agent 轨迹：随结果返回并落库，前端可展开看"这一步是谁做的、用了什么"
    record["agents"] = orchestration.as_dict()["trace"]
    record["agents"]["skipped"] = orchestration.skipped

    storage: dict[str, Any] = {}
    if persist_local:
        storage["local_dir"] = str(_persist_local(record, files, upload_dir or DEFAULT_UPLOAD_DIR))
    if persist_cloud:
        storage["cloud"] = _persist_cloud(record, run_id=resolved_run_id,
                                          si_upload=si_upload, bl_upload=bl_upload,
                                          duration_ms=duration_ms)
    record["storage"] = storage
    return record


async def _classify_upload_async(email: InboxEmail, *, client: Any | None,
                                 use_llm: bool) -> ClassificationResult:
    """上传件的分类：规则优先；规则不确定且允许 LLM 时交给 flash 档。"""
    ruled = rule_classify(email)
    if ruled is not None:
        return ruled
    if use_llm and client is not None:
        from .pipeline.classify import classify_email
        return await classify_email(email, client=client, use_rules=True, use_llm=True)
    return _classify_upload(email, client=client, use_llm=use_llm)


def _build_record(
    *,
    email: InboxEmail,
    classification: ClassificationResult,
    extraction: ExtractionResult | None,
    outcome: PolicyOutcome,
    report: ComparisonReport,
    si_view: Any,
    bl_view: Any,
    si_upload: UploadedFile | None,
    bl_upload: UploadedFile | None,
    run_id: str,
) -> dict[str, Any]:
    llm_used = bool(extraction is not None and extraction.model
                    and not str(extraction.model).startswith("rule"))
    # 用户主动上传就是要核对，因此提交口径固定为 BL_COMPARISON；
    # 分类器看到的类别作为诊断信息保留（Agent 叙事用），不参与裁决方向。
    status = outcome.status if (si_view is not None or bl_view is not None) else "NEEDS_REVIEW"
    defect_fields = sorted(outcome.defect_fields) if (si_view is not None and bl_view is not None) else []
    has_defect = bool(status == "MISMATCH" and defect_fields)
    review_reason = outcome.review_reason if status == "NEEDS_REVIEW" else None

    return {
        "email_id": email.email_id,
        "run_id": run_id,
        "from": email.sender,
        "subject": email.subject,
        "body": email.body,
        "attachments": list(email.attachments),
        "attachment_count": email.attachment_count,
        "category": CATEGORY_BL_COMPARISON,
        "status": status,
        "has_defect": has_defect,
        "defect_fields": defect_fields,
        "review_reason": review_reason,
        "review_reason_label": REASON_LABELS.get(review_reason or "", None),
        "decided_by": classification.decided_by,
        "rule_name": classification.rule_name,
        "category_confidence": round(classification.confidence, 3),
        "body_hint": classification.intent.doc_issue_hint,
        "si": _slot_view(si_view),
        "bl": _slot_view(bl_view),
        "fields": _field_rows(si_view, bl_view, report=report) if (si_view and bl_view) else [],
        "field_labels": FIELD_LABELS,
        # ---- 诊断侧信息：答辩时用来讲「AI 到底做了哪一步」 ----
        "classification": {
            "detected_category": classification.category,
            "confidence": round(classification.confidence, 3),
            "decided_by": classification.decided_by,
            "rule_name": classification.rule_name,
            "evidence_span": classification.evidence_span,
            "model": classification.model,
        },
        "extractor": {
            "mode": extraction.mode if extraction else "NONE",
            "model": extraction.model if extraction else "",
            "llm_used": llm_used,
            "from_cache": bool(extraction.from_cache) if extraction else False,
            "latency_ms": extraction.latency_ms if extraction else 0,
            "error": extraction.error if extraction else None,
            "si_readable": bool(si_view.is_readable) if si_view else False,
            "bl_readable": bool(bl_view.is_readable) if bl_view else False,
            "si_input": {"name": si_upload.name, "bytes": si_upload.size,
                         "sha256": si_upload.sha256} if si_upload else None,
            "bl_input": {"name": bl_upload.name, "bytes": bl_upload.size,
                         "sha256": bl_upload.sha256} if bl_upload else None,
        },
        "rationale": outcome.rationale,
    }


# ---------------------------------------------------------------------------
# 落盘 / 落云
# ---------------------------------------------------------------------------
def _persist_local(record: Mapping[str, Any], files: Sequence[UploadedFile],
                   upload_dir: Path) -> Path:
    """把上传件与结论存到本地审计目录（失败不影响返回结果）。"""
    target = Path(upload_dir) / str(record["run_id"])
    try:
        target.mkdir(parents=True, exist_ok=True)
        for upload in files:
            (target / _safe_name(upload.name)).write_bytes(upload.data)
        (target / "result.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    except OSError as exc:
        logger.warning("上传审计落盘失败：%s", exc)
    return target


def _persist_cloud(record: Mapping[str, Any], *, run_id: str,
                   si_upload: UploadedFile | None, bl_upload: UploadedFile | None,
                   duration_ms: int) -> Any:
    """写云端留痕。**任何失败都只记录，绝不让上传请求失败。**"""
    from .db.supabase import SupabaseUnavailable
    try:
        from .db.repo import record_upload_run
    except ImportError as exc:      # pragma: no cover
        return {"stored": False, "error": f"repo unavailable: {exc}"}
    extractor = record.get("extractor") or {}
    try:
        return record_upload_run(
            run_id=run_id, payload=_cloud_payload(record),
            subject=str(record.get("subject") or ""),
            si_name=si_upload.name if si_upload else None,
            bl_name=bl_upload.name if bl_upload else None,
            si_sha256=si_upload.sha256 if si_upload else None,
            bl_sha256=bl_upload.sha256 if bl_upload else None,
            si_bytes=si_upload.size if si_upload else None,
            bl_bytes=bl_upload.size if bl_upload else None,
            category=str(record.get("category") or CATEGORY_BL_COMPARISON),
            status=str(record.get("status") or "OK"),
            has_defect=bool(record.get("has_defect")),
            defect_fields=tuple(record.get("defect_fields") or ()),
            review_reason=record.get("review_reason"),
            extractor=str(extractor.get("model") or "rule"),
            mode=str(extractor.get("mode") or "PAIR"),
            llm_used=bool(extractor.get("llm_used")),
            duration_ms=duration_ms)
    except SupabaseUnavailable as exc:
        return {"stored": False, "skipped": str(exc)}
    except Exception as exc:      # noqa: BLE001 —— 云端是增强项，不能成为单点故障
        logger.warning("上传留痕写云失败：%s", exc)
        return {"stored": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


def _cloud_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    """给 cloud 的 payload 只留可展示的诊断信息（不塞整篇 PDF 文本）。"""
    return {
        "status": record.get("status"),
        "defect_fields": list(record.get("defect_fields") or []),
        "review_reason": record.get("review_reason"),
        "rationale": record.get("rationale"),
        "classification": record.get("classification"),
        "extractor": {key: value for key, value in (record.get("extractor") or {}).items()
                      if key not in {"si_input", "bl_input"}},
        # 多 Agent 轨迹也落库：复盘时能回答"这一步是谁做的、多久、用了哪个模型"
        "agents": record.get("agents"),
        "si": record.get("si"),
        "bl": record.get("bl"),
        "fields": [
            {key: row.get(key) for key in
             ("field", "si_raw", "bl_raw", "si_normalized", "bl_normalized",
              "is_match", "match_method", "similarity", "delta", "verdict")}
            for row in (record.get("fields") or [])
        ],
    }


# ---------------------------------------------------------------------------
# CLI：本地直接验一份文件对，不经过 Web
# ---------------------------------------------------------------------------
def _cli(argv: Sequence[str] | None = None) -> int:  # pragma: no cover
    import argparse
    import sys

    from .console import harden_console
    from .config import gemini_api_key_available

    harden_console()
    parser = argparse.ArgumentParser(description="上传即核对（命令行版）")
    parser.add_argument("files", nargs="+", help="SI 与 BL 文件路径（顺序无关，靠 _SI/_BL 或顺序推断）")
    parser.add_argument("--subject", default="")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--no-cloud", action="store_true")
    args = parser.parse_args(argv)

    uploads = [UploadedFile(name=Path(p).name, data=Path(p).read_bytes()) for p in args.files]
    client = None
    use_llm = not args.no_llm and gemini_api_key_available()
    if use_llm:
        from .llm.gemini import GeminiSettings, get_client
        client = get_client(GeminiSettings.from_env())

    record = asyncio.run(verify_uploaded_documents(
        uploads, subject=args.subject, use_llm=use_llm, client=client,
        persist_cloud=not args.no_cloud))

    print("=" * 78)
    print(f"结论：{record['status']} · 缺陷字段 {record['defect_fields'] or '（无）'}"
          f" · 理由 {record['review_reason'] or '（无）'}")
    print(f"抽取：mode={record['extractor']['mode']} model={record['extractor']['model']}"
          f" llm_used={record['extractor']['llm_used']}")
    print(f"耗时：{record['duration_ms']}ms · 存储：{record['storage']}")
    print("-" * 78)
    for row in record["fields"]:
        flag = {"defect": "[X]", "undecided": "[?]", "match": "[=]"}[row["verdict"]]
        print(f"{flag} {row['label']:<26} SI={row['si_raw']!r:<40} BL={row['bl_raw']!r}")
    return 0


if __name__ == "__main__":      # pragma: no cover
    import sys

    sys.exit(_cli())
