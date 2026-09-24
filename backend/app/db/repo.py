"""仓储层 —— 幂等写入四张表 + 读回官方提交产物。

设计要点（每一条都是踩过的坑）：
  1. **父表先落地**：comparisons / extracted_fields 都有 FK 指向 emails，
     顺序错了就是 23503 外键错误。写入顺序固定为 emails → extracted_fields
     → comparisons → review_queue。
  2. **attachment_count 是 GENERATED 列**：payload 里绝不能带它，否则 PostgreSQL
     直接报错。EmailWrite 根本不暴露这个字段就是最彻底的防呆。
  3. **on_conflict 必须与主键逐字一致**：emails=email_id、extracted_fields=email_id,doc_type、
     comparisons=email_id,field_name、review_queue=email_id,reason。
     写错不会报错，而是**插入重复行**，第二次跑批就把前端搞脏。
  4. **分块 + 失败隔离**：一批 100 行；单块失败只记该块 email_id，绝不丢整批。
  5. **阻塞调用走线程池**：SDK 是同步的，直接 await 会卡死事件循环。

本地没有 Supabase 时整个模块仍可 import（supabase.py 已做降级），
`upsert_*` 会抛 SupabaseUnavailable，由 runner 捕获后继续产出 submission.json。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from ..config import SUBMISSION_KEYS, REVIEW_REASONS
from ..contracts import validate_record
from ..pipeline.normalize import COMPARE_FIELDS
from .supabase import Gateway, SupabaseUnavailable, get_gateway

logger = logging.getLogger("sdoc.repo")

TABLE_EMAILS = "emails"
TABLE_EXTRACTIONS = "extracted_fields"
TABLE_COMPARISONS = "comparisons"
TABLE_REVIEW = "review_queue"
TABLE_UPLOADS = "upload_runs"
VIEW_SUBMISSION = "submission_view"
VIEW_UPLOAD_STATS = "upload_stats"

DEFAULT_CHUNK_SIZE = 100

_LOW_CONFIDENCE_THRESHOLD = 0.55


class RepoError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 写入载荷（与 runner 的构造点一一对应）
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class EmailWrite:
    """emails 表的一行。**刻意不含** attachment_count（GENERATED 列不可写）。"""

    email_id: str
    subject: str = ""
    body: str = ""
    from_addr: str = ""
    attachment_paths: list[str] = field(default_factory=list)
    category: str = "GENERAL"
    category_confidence: float = 0.0
    classified_by: str = "rule"
    body_hint: str | None = None
    verdict_status: str = "OK"
    has_defect: bool = False
    defect_fields: list[str] = field(default_factory=list)
    review_reason: str | None = None
    pipeline_state: str = "RECEIVED"
    prompt_version: str = ""
    model_classify: str | None = None
    latency_ms: int | None = None
    processed_at: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "email_id": self.email_id,
            "subject": self.subject or "",
            "body": self.body or "",
            "from_addr": self.from_addr or "",
            "attachment_paths": list(self.attachment_paths),
            "category": self.category,
            "category_confidence": round(float(self.category_confidence), 3),
            "classified_by": self.classified_by,
            "body_hint": self.body_hint,
            "verdict_status": self.verdict_status,
            "has_defect": bool(self.has_defect),
            "defect_fields": sorted(set(self.defect_fields or [])),
            "review_reason": self.review_reason,
            "pipeline_state": self.pipeline_state,
            "prompt_version": self.prompt_version or "",
            "model_classify": self.model_classify,
            "latency_ms": self.latency_ms,
            "processed_at": self.processed_at,
        }

    def verdict_record(self) -> dict[str, Any]:
        """转成官方 5 键形状，交给 contracts.validate_record 做发车自检。"""
        return {
            "category": self.category,
            "status": self.verdict_status,
            "review_reason": self.review_reason,
            "has_defect": bool(self.has_defect),
            "defect_fields": sorted(set(self.defect_fields or [])),
        }


@dataclass(slots=True)
class ExtractionWrite:
    """extracted_fields 表的一行（SI 与 BL 各一行，互不交叉）。"""

    email_id: str
    doc_type: str
    source_path: str = ""
    values: Mapping[str, Any] = field(default_factory=dict)
    field_confidence: Mapping[str, float] = field(default_factory=dict)
    field_blank: Sequence[str] = ()
    doc_role_detected: str = "UNKNOWN"
    other_doc_kind: str | None = None
    extractor_model: str = "rule"
    prompt_version: str = ""
    is_readable: bool = True
    read_error: str | None = None

    def to_row(self) -> dict[str, Any]:
        # values 必须**含全部 7 个键**（SQL 上有 values ?& 7字段 的约束），
        # 缺失的补 None：jsonb 里显式 null 仍算「键存在」，能满足 ?& 检查。
        values = {name: self.values.get(name) for name in COMPARE_FIELDS}
        confidence = {
            name: float(self.field_confidence.get(name, 0.0)) for name in COMPARE_FIELDS
        }
        return {
            "email_id": self.email_id,
            "doc_type": self.doc_type,
            "source_path": self.source_path or "",
            "values": {k: (None if v is None else str(v)) for k, v in values.items()},
            "field_confidence": confidence,
            "field_blank": sorted(set(self.field_blank or ())),
            "doc_role_detected": self.doc_role_detected or "UNKNOWN",
            "other_doc_kind": self.other_doc_kind,
            "extractor_model": self.extractor_model or "rule",
            "prompt_version": self.prompt_version or "",
            "is_readable": bool(self.is_readable),
            "read_error": self.read_error,
        }


@dataclass(slots=True)
class PipelineUnit:
    """一封邮件的完整写入单元（父行 + 子行）。"""

    email: EmailWrite
    extractions: tuple[ExtractionWrite, ...] = ()
    comparisons: tuple[Mapping[str, Any], ...] = ()


@dataclass(slots=True)
class RepoReport:
    emails_upserted: int = 0
    extractions_upserted: int = 0
    comparisons_upserted: int = 0
    review_upserted: int = 0
    pruned: int = 0
    failed_email_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failed_email_ids and self.skipped is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "emails": self.emails_upserted,
            "extractions": self.extractions_upserted,
            "comparisons": self.comparisons_upserted,
            "review_queue": self.review_upserted,
            "pruned": self.pruned,
            "failed": len(self.failed_email_ids),
            "errors": self.errors[:10],
            "skipped": self.skipped,
        }


# ---------------------------------------------------------------------------
# 行级构造
# ---------------------------------------------------------------------------
def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    return str(value)


def comparison_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """compare.FieldComparison.to_db_row() → 可直接 upsert 的行（Decimal 转 float）。"""
    allowed = {
        "email_id", "field_name", "si_raw", "bl_raw", "si_normalized", "bl_normalized",
        "si_parsed", "bl_parsed", "is_match", "match_method", "similarity", "delta",
        "needs_human", "needs_human_reason", "highlight", "decided_by", "note",
    }
    out = {key: _json_safe(value) for key, value in row.items() if key in allowed}
    if "email_id" not in out or "field_name" not in out:
        raise RepoError(f"comparison row missing primary key: {sorted(row)}")
    if out.get("match_method") is None:
        raise RepoError("comparison row missing match_method (SQL CHECK constraint)")
    reason = out.get("needs_human_reason")
    if reason is not None and reason not in REVIEW_REASONS:
        out["needs_human_reason"] = None      # 脏理由绝不允许写库（枚举会拒收）
    out.setdefault("highlight", [])
    out.setdefault("decided_by", "rule")
    out.setdefault("si_parsed", {})
    out.setdefault("bl_parsed", {})
    return out


def _review_rows(unit: PipelineUnit) -> list[dict[str, Any]]:
    """升级工单：只有 NEEDS_REVIEW 才建单，priority 越小越急。"""
    email = unit.email
    if email.verdict_status != "NEEDS_REVIEW":
        return []
    reason = email.review_reason
    if reason not in REVIEW_REASONS:
        return []
    priority = {
        "missing_attachment": 10,
        "missing_value": 20,
        "unreadable": 30,
        "wrong_doc_type": 40,
    }.get(reason, 50)
    if email.category_confidence < _LOW_CONFIDENCE_THRESHOLD:
        priority = min(priority, 25)
    return [{
        "email_id": email.email_id,
        "reason": reason,
        "priority": priority,
        "field_names": sorted(set(email.defect_fields or [])),
        "status": "OPEN",
    }]


# ---------------------------------------------------------------------------
# 校验：写库前先过官方合约，脏数据不落地
# ---------------------------------------------------------------------------
def validate_units(units: Sequence[PipelineUnit]) -> list[str]:
    problems: list[str] = []
    for unit in units:
        for problem in validate_record(unit.email.verdict_record()):
            problems.append(f"{unit.email.email_id}: {problem}")
        for extraction in unit.extractions:
            if extraction.doc_type not in ("SI", "BL"):
                problems.append(f"{extraction.email_id}: invalid doc_type={extraction.doc_type!r}")
        for row in unit.comparisons:
            if row.get("field_name") not in COMPARE_FIELDS:
                problems.append(f"{unit.email.email_id}: invalid field name {row.get('field_name')!r}")
        if len(problems) > 40:
            break
    return problems


# ---------------------------------------------------------------------------
# 主写入路径
# ---------------------------------------------------------------------------
def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), max(1, size)):
        yield items[start:start + size]


def _execute(query: Any) -> Any:
    response = query.execute()
    data = getattr(response, "data", None)
    return data if isinstance(data, list) else []


async def upsert_verification_pipeline_results(
    units: Sequence[PipelineUnit],
    *,
    gateway: Gateway | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    prune_email_ids: Sequence[str] | None = None,
    validate: bool = True,
) -> RepoReport:
    """一次性把邮件状态 / 7 字段抽取 / 字段级比对写进 Supabase（幂等）。

    参数
        units            每封邮件一个单元（父行 + SI/BL 抽取 + 7 行比对）
        prune_email_ids  本次跑批覆盖的**全部** email_id；不在 units 里的那些
                         会被清理掉旧的 comparisons 与未结工单，避免重跑后残留脏行。
        返回            RepoReport（含失败隔离明细）
    """
    report = RepoReport()
    if not units:
        report.skipped = "没有可写入的单元"
        return report

    if validate:
        problems = validate_units(units)
        if problems:
            raise RepoError("payload violates the official 5-key contract, refusing to write: " + "; ".join(problems[:5]))

    gw = gateway or get_gateway()
    try:
        gw.client                                    # 触发建连，失败尽早暴露
    except SupabaseUnavailable as exc:
        report.skipped = str(exc)
        logger.warning("跳过落库：%s", exc)
        return report

    # ---- 1) 父表：emails ----
    parent_rows = [unit.email.to_row() for unit in units]
    try:
        for chunk in _chunks(parent_rows, chunk_size):
            await gw.arun(
                lambda rows=list(chunk): _execute(
                    gw.table(TABLE_EMAILS).upsert(rows, on_conflict="email_id")),
                label="upsert:emails")
            report.emails_upserted += len(chunk)
    except Exception as exc:  # noqa: BLE001 —— 父表写不进则整批放弃（子表无 FK 可依）
        report.failed_email_ids.extend(unit.email.email_id for unit in units)
        report.errors.append(f"emails: {type(exc).__name__}: {exc}"[:300])
        logger.error("emails 写入失败，终止本批：%s", exc)
        return report

    # ---- 2) extracted_fields ----
    extraction_rows = [e.to_row() for unit in units for e in unit.extractions]
    if extraction_rows:
        await _upsert_child(gw, TABLE_EXTRACTIONS, extraction_rows, "email_id,doc_type",
                            report, "extractions_upserted", chunk_size,
                            fallback_ids=[u.email.email_id for u in units])

    # ---- 3) comparisons ----
    comparison_rows = [comparison_row(row) for unit in units for row in unit.comparisons]
    if comparison_rows:
        await _upsert_child(gw, TABLE_COMPARISONS, comparison_rows, "email_id,field_name",
                            report, "comparisons_upserted", chunk_size,
                            fallback_ids=[u.email.email_id for u in units])

    # ---- 4) review_queue ----
    review_rows = [row for unit in units for row in _review_rows(unit)]
    if review_rows:
        await _upsert_child(gw, TABLE_REVIEW, review_rows, "email_id,reason",
                            report, "review_upserted", chunk_size,
                            fallback_ids=[u.email.email_id for u in units])

    # ---- 5) 清理：本轮未产生比对行的邮件，删掉旧比对与未结工单 ----
    if prune_email_ids is not None:
        covered = {unit.email.email_id for unit in units}
        stale = [eid for eid in prune_email_ids if eid not in covered]
        report.pruned = await _prune(gw, stale)

    return report


async def _upsert_child(
    gw: Gateway,
    table: str,
    rows: Sequence[Mapping[str, Any]],
    on_conflict: str,
    report: RepoReport,
    counter: str,
    chunk_size: int,
    *,
    fallback_ids: Sequence[str],
) -> None:
    """子表分块 upsert，单块失败只影响该块。"""
    for chunk in _chunks(rows, chunk_size):
        try:
            await gw.arun(
                lambda t=table, r=list(chunk), oc=on_conflict: _execute(
                    gw.table(t).upsert(r, on_conflict=oc)),
                label=f"upsert:{table}")
            setattr(report, counter, getattr(report, counter) + len(chunk))
        except Exception as exc:  # noqa: BLE001
            ids = sorted({str(row.get("email_id")) for row in chunk})
            report.failed_email_ids.extend(ids or list(fallback_ids))
            report.errors.append(f"{table}: {type(exc).__name__}: {exc}"[:300])
            logger.error("%s 写入失败（%d 行）：%s", table, len(chunk), exc)


async def _prune(gw: Gateway, stale_ids: Sequence[str]) -> int:
    if not stale_ids:
        return 0
    removed = 0
    for table, extra in ((TABLE_COMPARISONS, None), (TABLE_REVIEW, ("status", "OPEN"))):
        for chunk in _chunks(list(stale_ids), 50):
            try:
                await gw.arun(
                    lambda t=table, ids=list(chunk), e=extra: _execute(
                        (gw.table(t).delete().in_("email_id", ids)
                         .eq(e[0], e[1]) if e else gw.table(t).delete().in_("email_id", ids))),
                    label=f"prune:{table}")
                removed += len(chunk)
            except Exception as exc:  # noqa: BLE001
                logger.warning("清理 %s 失败：%s", table, exc)
    return removed


# ---------------------------------------------------------------------------
# 上传留痕（「上传即核对」的云端一侧）
#
# 为什么要单独一条写入路径：上传件**绝不能**进 emails 表。submission_view 是从
# emails 聚合的 520 键集合，多一个未知键就会被官方算 penalty —— 用户随手上传
# 一份文件就把满分打没了。因此上传只写 upload_runs，与评测数据物理隔离。
# ---------------------------------------------------------------------------
def record_upload_run(
    *,
    run_id: str,
    payload: Mapping[str, Any],
    subject: str = "",
    si_name: str | None = None,
    bl_name: str | None = None,
    si_sha256: str | None = None,
    bl_sha256: str | None = None,
    si_bytes: int | None = None,
    bl_bytes: int | None = None,
    category: str = "BL_COMPARISON",
    status: str = "OK",
    has_defect: bool = False,
    defect_fields: Sequence[str] = (),
    review_reason: str | None = None,
    extractor: str = "rule",
    mode: str = "PAIR",
    llm_used: bool = False,
    duration_ms: int | None = None,
    gateway: Gateway | None = None,
) -> dict[str, Any]:
    """把一次上传核对写入云端（幂等 upsert）。云端不可用时抛 SupabaseUnavailable。"""
    if status not in ("OK", "MISMATCH", "NEEDS_REVIEW"):
        raise RepoError(f"invalid status={status!r}")
    invalid = sorted(set(defect_fields) - set(COMPARE_FIELDS))
    if invalid:
        raise RepoError(f"invalid field names {invalid}")
    reason = review_reason if review_reason in REVIEW_REASONS else None
    row = {
        "run_id": run_id,
        "subject": subject or "",
        "si_name": si_name, "bl_name": bl_name,
        "si_sha256": si_sha256, "bl_sha256": bl_sha256,
        "si_bytes": si_bytes, "bl_bytes": bl_bytes,
        "category": category or "BL_COMPARISON",
        "status": status,
        "has_defect": bool(has_defect),
        "defect_fields": sorted(set(defect_fields)),
        "review_reason": reason,
        "extractor": extractor or "rule",
        "mode": mode or "PAIR",
        "llm_used": bool(llm_used),
        "duration_ms": duration_ms,
        "payload": _json_safe(dict(payload)),
    }
    gw = gateway or get_gateway()
    gw.run(lambda: _execute(gw.table(TABLE_UPLOADS)
                            .upsert([row], on_conflict="run_id")),
           label="upsert:upload_runs")
    return {"run_id": run_id, "stored": "supabase", "status": status}


async def arecord_upload_run(**kwargs: Any) -> dict[str, Any]:
    return await asyncio.to_thread(lambda: record_upload_run(**kwargs))


def list_upload_runs(*, limit: int = 20, gateway: Gateway | None = None) -> list[dict[str, Any]]:
    """读回最近的上传核对（前端「最近核对」面板）。"""
    gw = gateway or get_gateway()
    rows = gw.run(
        lambda: _execute(gw.table(TABLE_UPLOADS)
                         .select("run_id,created_at,subject,si_name,bl_name,category,status,"
                                 "has_defect,defect_fields,review_reason,extractor,mode,"
                                 "llm_used,duration_ms")
                         .order("created_at", desc=True).limit(limit)),
        label="fetch:upload_runs")
    return list(rows or [])


def upload_stats(gateway: Gateway | None = None) -> dict[str, Any]:
    gw = gateway or get_gateway()
    rows = gw.run(lambda: _execute(gw.table(VIEW_UPLOAD_STATS).select("*").limit(1)),
                  label="fetch:upload_stats")
    return (rows[0] if rows else {}) or {}


# ---------------------------------------------------------------------------
# 读回：官方提交产物
# ---------------------------------------------------------------------------
def fetch_submission(
    gateway: Gateway | None = None,
    *,
    limit: int | None = None,
) -> dict[str, dict[str, Any]]:
    """从 submission_view 读出官方 5 键产物（人工改判已自动生效）。

    同步接口：CLI / 测试脚本用；服务端请用 afetch_submission。
    """
    gw = gateway or get_gateway()
    rows: list[dict[str, Any]] = []
    page = 1000
    offset = 0
    while True:
        size = min(page, limit - offset) if limit else page
        if size <= 0:
            break
        batch = gw.run(
            lambda o=offset, s=size: _execute(
                gw.table(VIEW_SUBMISSION)
                .select("email_id,category,status,review_reason,has_defect,defect_fields,"
                        "classified_by,pipeline_state,manually_reviewed")
                .order("email_id").range(o, o + s - 1)),
            label="fetch_submission")
        rows.extend(batch)
        if len(batch) < size:
            break
        offset += size
        if limit and offset >= limit:
            break

    submission: dict[str, dict[str, Any]] = {}
    for row in rows:
        email_id = str(row.get("email_id"))
        submission[email_id] = {
            "category": row.get("category") or "GENERAL",
            "status": row.get("status") or "OK",
            "review_reason": row.get("review_reason"),
            "has_defect": bool(row.get("has_defect")),
            "defect_fields": sorted(set(row.get("defect_fields") or [])),
            **({"decided_by": row.get("classified_by") or "rule"}
               if row.get("classified_by") else {}),
        }
    return submission


async def afetch_submission(
    gateway: Gateway | None = None,
    *,
    limit: int | None = None,
) -> dict[str, dict[str, Any]]:
    return await asyncio.to_thread(fetch_submission, gateway, limit=limit)


def fetch_emails_page(
    *,
    email_ids: Sequence[str] | None = None,
    gateway: Gateway | None = None,
) -> dict[str, dict[str, Any]]:
    """读取 emails 表的审计展示行（subject/from/附件名 + 权威结论）。

    这是「云端审计页」的数据源：API 层据此在**没有本地快照**的环境
    （例如 Railway 容器里没有官方 data/ 目录）仍然能渲染完整工作台。

    附件只回传**文件名**（与本地快照的展示口径一致），不回传路径——
    路径在云端容器里无意义，泄露内部目录结构也没有任何价值。
    `email_ids` 提供时只取这些行（详情端点用），否则全量（列表端点用）。
    """
    gw = gateway or get_gateway()
    rows: list[dict[str, Any]] = []
    page = 1000
    if email_ids:
        ids = list(dict.fromkeys(email_ids))          # 去重且保持顺序
        for start in range(0, len(ids), page):
            batch = gw.run(
                lambda c=ids[start:start + page]: _execute(
                    gw.table(TABLE_EMAILS)
                    .select("email_id,subject,from_addr,attachment_paths,category,verdict_status,"
                            "has_defect,defect_fields,review_reason,manual_category,manual_status,"
                            "manual_review_reason,manual_defect_fields,classified_by")
                    .in_("email_id", c)),
                label="fetch_emails_page")
            rows.extend(batch)
    else:
        offset = 0
        while True:
            batch = gw.run(
                lambda o=offset: _execute(
                    gw.table(TABLE_EMAILS)
                    .select("email_id,subject,from_addr,attachment_paths,category,verdict_status,"
                            "has_defect,defect_fields,review_reason,manual_category,manual_status,"
                            "manual_review_reason,manual_defect_fields,classified_by")
                    .order("email_id").range(o, o + page - 1)),
                label="fetch_emails_page")
            rows.extend(batch)
            if len(batch) < page:
                break
            offset += page

    emails: dict[str, dict[str, Any]] = {}
    for row in rows:
        overridden = row.get("manual_status") is not None or row.get("manual_category") is not None
        emails[str(row.get("email_id"))] = {
            "email_id": str(row.get("email_id")),
            "subject": row.get("subject") or "",
            "from": row.get("from_addr") or "",
            "attachments": [str(path).rsplit("/", 1)[-1]
                            for path in (row.get("attachment_paths") or [])],
            "attachment_count": len(row.get("attachment_paths") or []),
            "category": row.get("category") or "GENERAL",
            "status": row.get("verdict_status") or "OK",
            "has_defect": bool(row.get("has_defect")),
            "defect_fields": sorted(set(row.get("defect_fields") or [])),
            "review_reason": row.get("review_reason"),
            "manual_category": row.get("manual_category"),
            "manual_status": row.get("manual_status"),
            "manual_review_reason": row.get("manual_review_reason"),
            "manual_defect_fields": sorted(set(row.get("manual_defect_fields") or [])),
            "decided_by": row.get("classified_by") or "rule",
            "overridden": overridden,
        }
    return emails


async def afetch_emails_page(
    *,
    email_ids: Sequence[str] | None = None,
    gateway: Gateway | None = None,
) -> dict[str, dict[str, Any]]:
    return await asyncio.to_thread(fetch_emails_page, email_ids=email_ids, gateway=gateway)


def validate_submission(
    submission: Mapping[str, Mapping[str, Any]],
    *,
    expected_ids: Sequence[str] | None = None,
) -> list[str]:
    """提交前自检：键集完整性 + 每条记录的官方合约不变量。"""
    problems: list[str] = []
    if expected_ids is not None:
        expected = set(expected_ids)
        missing = sorted(expected - set(submission))
        extra = sorted(set(submission) - expected)
        if missing:
            problems.append(f"missing {len(missing)} email_id(s) (official scoring counts missing keys as GENERAL): "
                            f"{missing[:5]}")
        if extra:
            problems.append(f"extra {len(extra)} unknown email_id(s): {extra[:5]}")
    for email_id, record in submission.items():
        extra_keys = set(record) - SUBMISSION_KEYS - {"decided_by"}
        if extra_keys:
            problems.append(f"{email_id}: extra keys {sorted(extra_keys)} (official accepts exactly 5 keys)")
        for problem in validate_record(dict(record)):
            problems.append(f"{email_id}: {problem}")
        if len(problems) > 40:
            break
    return problems


def strip_diagnostics(submission: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """剥离 decided_by 等诊断键，得到严格 5 键的提交产物。"""
    return {email_id: {key: value for key, value in record.items() if key in SUBMISSION_KEYS}
            for email_id, record in submission.items()}


# ---------------------------------------------------------------------------
# 人审改判（第 4 步闭环）
# ---------------------------------------------------------------------------
def apply_manual_verdict(
    email_id: str,
    *,
    status: str,
    defect_fields: Sequence[str] = (),
    review_reason: str | None = None,
    note: str | None = None,
    reviewer: str | None = None,
    gateway: Gateway | None = None,
) -> dict[str, Any]:
    """调用 RPC 写回人工结论。校验在 SQL 端再走一遍，前端无法绕过。"""
    if status not in ("OK", "MISMATCH", "NEEDS_REVIEW"):
        raise RepoError(f"invalid status={status!r}")
    invalid = sorted(set(defect_fields) - set(COMPARE_FIELDS))
    if invalid:
        raise RepoError(f"invalid field names {invalid}; allowed: the 7 canonical fields")
    gw = gateway or get_gateway()
    payload = {
        "p_email_id": email_id,
        "p_status": status,
        "p_defect_fields": sorted(set(defect_fields)),
        "p_review_reason": review_reason,
        "p_note": note,
        "p_reviewer": reviewer,
    }
    result = gw.run(lambda: gw.rpc("apply_manual_verdict", payload).execute(),
                    label="rpc:apply_manual_verdict")
    data = getattr(result, "data", None)
    return {"email_id": email_id, "applied": True,
            "row": (data[0] if isinstance(data, list) and data else data)}


def revert_manual_verdict(
    email_id: str, *, reviewer: str | None = None, gateway: Gateway | None = None,
) -> dict[str, Any]:
    gw = gateway or get_gateway()
    result = gw.run(
        lambda: gw.rpc("revert_manual_verdict",
                       {"p_email_id": email_id, "p_reviewer": reviewer}).execute(),
        label="rpc:revert_manual_verdict")
    data = getattr(result, "data", None)
    return {"email_id": email_id, "reverted": True,
            "row": (data[0] if isinstance(data, list) and data else data)}


def mark_submitted(
    email_ids: Sequence[str] | None = None, *, gateway: Gateway | None = None,
) -> int:
    """提交成功后把整批置为 SUBMITTED（演示时进度可见）。"""
    gw = gateway or get_gateway()
    result = gw.run(
        lambda: gw.rpc("mark_submitted",
                       {"p_email_ids": list(email_ids) if email_ids else None}).execute(),
        label="rpc:mark_submitted")
    data = getattr(result, "data", None)
    if isinstance(data, int):
        return data
    if isinstance(data, list) and data:
        first = data[0]
        return int(first.get("mark_submitted", 0)) if isinstance(first, dict) else int(first)
    return 0
