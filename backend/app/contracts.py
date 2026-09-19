"""★ 冻结件：全队唯一接口语言。

四个人的代码都 import 这里。改动必须走 PR 并 @ 全员。
注意：compare.py 的 DocumentView / FieldComparison / ComparisonReport 同属冻结接口，
但它们放在 compare.py（与该算法同址），避免循环 import。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .config import REVIEW_REASONS, STATUSES, SUBMISSION_KEYS

# ---------------------------------------------------------------------------
# 官方枚举（闭集，与 db/migrations/0001_init.sql 的 PG enum 逐字一致）
# ---------------------------------------------------------------------------
Category = Literal["BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"]
VerdictStatus = Literal["OK", "MISMATCH", "NEEDS_REVIEW"]
ReviewReason = Literal["wrong_doc_type", "missing_attachment", "unreadable", "missing_value"]
DocType = Literal["SI", "BL"]
DocRole = Literal["SI", "BL", "OTHER", "UNKNOWN"]
MatchMethod = Literal[
    "exact", "normalized", "alias", "numeric_tolerance", "fuzzy", "missing", "not_compared"
]
DecidedBy = Literal["rule", "llm", "hybrid", "human"]
PipelineState = Literal[
    "RECEIVED", "CLASSIFIED", "PARSED", "EXTRACTED", "COMPARED",
    "ESCALATED", "FAILED", "SKIPPED",
]
QueueState = Literal["OPEN", "IN_REVIEW", "RESOLVED", "DISMISSED"]

__all__ = [
    "Category", "VerdictStatus", "ReviewReason", "DocType", "DocRole", "MatchMethod",
    "DecidedBy", "PipelineState", "QueueState", "SUBMISSION_KEYS", "CATEGORIES",
    "STATUSES", "REVIEW_REASONS", "VerdictRecord", "ReviewItem", "validate_record",
]

SUBMISSION_KEYS = SUBMISSION_KEYS
CATEGORIES = ("BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM")
STATUSES = STATUSES
REVIEW_REASONS = REVIEW_REASONS


# ---------------------------------------------------------------------------
# 官方 5 键提交记录
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class VerdictRecord:
    """官方提交产物的单条记录。键集必须与 sample_submission.json 完全一致。"""

    category: Category = "GENERAL"
    status: VerdictStatus = "OK"
    review_reason: ReviewReason | None = None
    has_defect: bool = False
    defect_fields: list[str] = field(default_factory=list)

    def to_submission(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "status": self.status,
            "review_reason": self.review_reason,
            "has_defect": bool(self.has_defect),
            "defect_fields": sorted(set(self.defect_fields)),
        }

    @classmethod
    def empty(cls) -> "VerdictRecord":
        return cls()


def validate_record(record: dict[str, Any]) -> list[str]:
    """校验单条记录是否符合官方合约的不变量。返回问题列表（空即通过）。"""
    problems: list[str] = []
    keys = set(record)
    if keys - SUBMISSION_KEYS - {"decided_by"}:
        problems.append(f"多余键 {sorted(keys - SUBMISSION_KEYS - {'decided_by'})}")
    if not SUBMISSION_KEYS.issubset(keys):
        problems.append(f"缺少键 {sorted(SUBMISSION_KEYS - keys)}")
        return problems

    category = record["category"]
    status = record["status"]
    reason = record["review_reason"]
    has_defect = bool(record["has_defect"])
    fields = list(record["defect_fields"] or [])

    if category not in CATEGORIES:
        problems.append(f"非法 category={category!r}")
    if status not in STATUSES:
        problems.append(f"非法 status={status!r}")
    if reason is not None and reason not in REVIEW_REASONS:
        problems.append(f"非法 review_reason={reason!r}")

    # 官方口径：三种状态互斥且完备（与 PostgreSQL 的 sdoc_verdict_shape_chk 同义）
    if status == "MISMATCH":
        if not has_defect or not fields:
            problems.append("MISMATCH 必须 has_defect=true 且 defect_fields 非空")
        if reason is not None:
            problems.append("MISMATCH 不允许携带 review_reason")
    elif status == "OK":
        if has_defect or fields:
            problems.append("OK 不允许携带缺陷")
        if reason is not None:
            problems.append("OK 不允许携带 review_reason")
    elif status == "NEEDS_REVIEW":
        if has_defect or fields:
            problems.append("NEEDS_REVIEW 不允许携带缺陷")
        if reason is None:
            problems.append("NEEDS_REVIEW 必须给出 review_reason")
    return problems


# ---------------------------------------------------------------------------
# 人工审核队列条目（前端 D 与后端通信的契约）
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ReviewItem:
    email_id: str
    reason: ReviewReason
    priority: int = 50
    state: QueueState = "OPEN"
    triggered_by: Literal["policy", "low_confidence", "manual"] = "policy"
    trigger_detail: dict[str, Any] = field(default_factory=dict)
    confidence_min: float | None = None
    rationale: str = ""
    resolution_status: VerdictStatus | None = None
    resolved_defect_fields: list[str] = field(default_factory=list)
    reviewer_note: str | None = None
