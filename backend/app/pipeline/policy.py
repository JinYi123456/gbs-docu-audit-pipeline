"""STEP 4 —— 升级阶梯（Ask for help 的唯一决策点）。

⚠️ 本文件归 C（比对 Lead）所有，当前为接口冻结版参考实现，C 可原样接管。

顺序即优先级，第一条命中即停。第 1 条是铁律：
    只要检出 ≥1 个字段不一致 → MISMATCH，绝不升级。
官方 final_score 的 0.50 全押在 46 封缺陷邮件的 defect_fields 精确集合上，
对这 46 封的任何一次误升级都是净亏损（每封 1.087 分）。

真实的 91 封「无附件但 GT=OK」邮件正文形如
    "Please assist to send the draft BL for <ref> for checking asap."
即"请帮我把草稿 BL 发过来"，没有可比对对象 → 必须判 OK，不能升级。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

REASON_WRONG_DOC_TYPE: Final[str] = "wrong_doc_type"
REASON_MISSING_ATTACHMENT: Final[str] = "missing_attachment"
REASON_UNREADABLE: Final[str] = "unreadable"
REASON_MISSING_VALUE: Final[str] = "missing_value"

# 低置信度阈值：仅用于触发「进人工队列」标记，绝不改变 OK / MISMATCH 结论
LOW_CONFIDENCE_THRESHOLD: Final[float] = 0.55


@dataclass(slots=True, frozen=True)
class EscalationContext:
    """来自 runner 的上下文：附件形态 + 正文提示（Prompt A 的侧信道）。"""

    attachment_count: int
    si_present: bool
    bl_present: bool
    body_asserts_documents_attached: bool = False
    body_hint: str | None = None
    min_field_confidence: float = 1.0


@dataclass(slots=True, frozen=True)
class PolicyOutcome:
    status: str
    has_defect: bool
    defect_fields: tuple[str, ...]
    review_reason: str | None
    needs_review_queue: bool
    triggered_by: str = "policy"
    rationale: str = ""


def _escalate(reason: str, rationale: str, *, triggered_by: str = "policy") -> PolicyOutcome:
    return PolicyOutcome(
        status="NEEDS_REVIEW", has_defect=False, defect_fields=(),
        review_reason=reason, needs_review_queue=True, triggered_by=triggered_by,
        rationale=rationale,
    )


def apply_policy(report: Any, context: EscalationContext) -> PolicyOutcome:
    """把 compare.ComparisonReport 的文档级结论与附件/正文证据合并为最终裁决。"""
    # 规则 1（铁律）—— 已确认的不一致 > 一切升级信号
    if report.defect_fields:
        return PolicyOutcome(
            status="MISMATCH", has_defect=True,
            defect_fields=tuple(sorted(report.defect_fields)),
            review_reason=None, needs_review_queue=False,
            rationale="Field-level mismatch confirmed; undecided fields never override it",
        )

    # 规则 2 —— 附件不是 SI/BL 对照件（第二个附件是发票 / 装箱单 / COO）
    if report.review_reason == REASON_WRONG_DOC_TYPE \
            or context.body_hint == REASON_WRONG_DOC_TYPE:
        return _escalate(REASON_WRONG_DOC_TYPE,
                         "The second attachment was judged not to be a draft BL")

    # 规则 3 —— 该附却没附：0 附件且正文声称应附，或只有单侧附件
    if context.attachment_count == 0:
        if context.body_asserts_documents_attached \
                or context.body_hint == REASON_MISSING_ATTACHMENT:
            return _escalate(REASON_MISSING_ATTACHMENT,
                             "The email states documents are attached but none are")
        return PolicyOutcome(
            status="OK", has_defect=False, defect_fields=(), review_reason=None,
            needs_review_queue=False,
            rationale="No attachment and the mail only requests a draft BL — nothing to compare",
        )
    if context.attachment_count == 1 and not (context.si_present and context.bl_present):
        return _escalate(REASON_MISSING_ATTACHMENT,
                         "Only one side is attached; the counterpart document is missing")

    # 规则 4 —— 文档不可读（空文件 / 图片型扫描件 / 无文本层）
    if report.review_reason == REASON_UNREADABLE \
            or not report.escalation_signals.get("si_readable", True) \
            or not report.escalation_signals.get("bl_readable", True):
        return _escalate(REASON_UNREADABLE,
                         "Document has no readable text layer or is corrupt")

    # 规则 5 —— 两侧无冲突但存在空白字段（占位符是不确定，不是不一致）
    if report.undecided_fields or report.review_reason == REASON_MISSING_VALUE:
        return _escalate(REASON_MISSING_VALUE,
                         f"Required fields blank or placeholders: "
                         f"{list(report.undecided_fields)[:7]}")

    # 规则 6 —— 低置信度：只入人工队列，不改结论
    if context.min_field_confidence < LOW_CONFIDENCE_THRESHOLD:
        return PolicyOutcome(
            status="OK", has_defect=False, defect_fields=(), review_reason=None,
            needs_review_queue=True, triggered_by="low_confidence",
            rationale=f"Lowest field confidence {context.min_field_confidence:.2f} is below the "
                      f"review threshold",
        )

    # 规则 7 —— 全部一致
    return PolicyOutcome(
        status="OK", has_defect=False, defect_fields=(), review_reason=None,
        needs_review_queue=False, rationale="All 7 canonical fields match",
    )


def queue_priority(reason: str, *, min_confidence: float = 1.0) -> int:
    base = {
        REASON_WRONG_DOC_TYPE: 10,
        REASON_MISSING_VALUE: 20,
        REASON_UNREADABLE: 30,
        REASON_MISSING_ATTACHMENT: 0,
    }.get(reason, 40)
    return max(0, min(100, 50 + base + int((1 - min_confidence) * 20)))
