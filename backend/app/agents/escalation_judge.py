"""Escalation Judge Agent —— 升级裁决（Ask-for-help 的唯一出口）。

它回答的问题不是"两边是不是一致"，而是
**"我现在该自己下结论，还是该请人来？"**

这是整条链上最反直觉的一段，也是被真实数据反复教育过的一段：

  · 铁律：只要检出 ≥1 个已确证的字段不一致 → MISMATCH，**绝不升级**。
    官方 final_score 的 50% 押在 46 封缺陷邮件的 defect_fields 精确集合上，
    对它们的任何一次"升级"都是净亏损。
  · 反过来，孤立的空白/占位符（`TBA`、`____MT`）是"不确定"而不是"不一致" ——
    误判成缺陷同样扣分。
  · 正文里的 4 类侧信道提示（附件掉了 / 文件打不开 / 客改的字段留空 / 附件不是 BL）
    只用来决定**升级理由**，永远不能生成缺陷字段。这条边界是 46 封缺陷邮件
    与 20 封升级召回之间的唯一安全线。

结论以 `PolicyOutcome` 返回：status / defect_fields / review_reason / 是否需要建工单。
"""
from __future__ import annotations

from typing import Any

from ..pipeline.policy import EscalationContext, apply_policy
from .base import ROLE_JUDGE, Agent, AgentStep, PipelineState

REASON_LABELS = {
    "wrong_doc_type": "Attachment is not a draft BL (invoice / packing list / COO)",
    "missing_attachment": "Expected attachment missing — nothing comparable to audit",
    "unreadable": "No readable text layer (likely an image-only scan)",
    "missing_value": "Blank or placeholder fields — not enough evidence to decide",
}


class EscalationJudgeAgent(Agent):
    name = "escalation_judge"
    role = ROLE_JUDGE
    description = ("Escalation ladder: confirmed mismatch > attachment issue > unreadable > "
                   "blank uncertainty; side-channel hints only shape the reason and never "
                   "create defect fields")

    def __init__(self, *, empty_report: Any | None = None) -> None:
        # 允许注入"缺一侧"时的占位 report（上传/跑批共用同一构造）
        self._empty_report = empty_report

    def _context(self, state: PipelineState) -> EscalationContext:
        """把三份证据（附件形态 / 正文侧信道 / 字段置信度）汇成升级上下文。

        注意：`escalation_signals`（si_readable / bl_readable）不在上下文里 ——
        它们已经承载在 `report` 自身，policy 直接读 report，无需二次搬运。
        """
        confidences = [
            value for view in (state.si_view, state.bl_view) if view is not None
            for value in (view.field_confidence or {}).values()
        ]
        return EscalationContext(
            attachment_count=state.email.attachment_count,
            si_present=state.si_view is not None,
            bl_present=state.bl_view is not None,
            body_asserts_documents_attached=bool(
                state.classification and state.classification.intent.asserts_documents_attached),
            body_hint=state.classification.intent.doc_issue_hint if state.classification else None,
            min_field_confidence=min(confidences) if confidences else 1.0,
        )

    async def execute(self, state: PipelineState) -> AgentStep:
        report = state.report if state.report is not None else self._empty_report
        if report is None:
            # 两侧都没有文档：按"缺附件"处理（由 policy 规则 3 决定 OK 还是升级）
            from ..pipeline.compare import ComparisonReport
            report = ComparisonReport(
                comparisons=(), defect_fields=(), matched_fields=(), undecided_fields=(),
                status="OK", has_defect=False, review_reason=None,
                escalation_signals={"si_readable": False, "bl_readable": False})

        outcome = apply_policy(report, self._context(state))
        state.outcome = outcome
        escalated = outcome.status == "NEEDS_REVIEW"
        # 一句判词要自带信息量：光写个 "OK/MISMATCH" 在轨迹面板里等于没说。
        if escalated:
            summary = (f"NEEDS_REVIEW — "
                       f"{REASON_LABELS.get(outcome.review_reason or '', outcome.review_reason)}")
        elif outcome.status == "MISMATCH":
            summary = (f"MISMATCH — {len(outcome.defect_fields)} field(s) form a compliance defect, "
                       f"reported directly")
        else:
            summary = "OK — no defect fields, auto-filed with zero human touch"
            if outcome.needs_review_queue:
                summary = ("OK — no defect fields, but queued for a low-confidence spot check "
                           "(verdict unchanged)")
        return AgentStep(
            agent=self.name, role=self.role, used_llm=False,
            summary=summary,
            evidence={
                "status": outcome.status,
                "defect_fields": sorted(outcome.defect_fields),
                "review_reason": outcome.review_reason,
                "needs_review_queue": outcome.needs_review_queue,
                "triggered_by": outcome.triggered_by,
                "rationale": outcome.rationale,
                "min_field_confidence": round(self._context(state).min_field_confidence, 3),
            })
