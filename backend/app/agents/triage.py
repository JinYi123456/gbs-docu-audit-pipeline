"""Triage Agent —— 语义分流（闸门）。决定"这封邮件要不要进比对流程"。

为什么它必须先跑、且必须是闸门：520 封里有 40 封垃圾邮件、9 封询价、大量内部
运维通报。对非对照类邮件跑比对不仅浪费，还会**制造假缺陷**（把发票的字段
当成缺失的 BL 字段）。Stage-1 的 30% 权重就压在这一个判断上。

两档策略（这是"AI 核心"的具体体现，而不是修辞）：
  1. **确定性规则优先** —— 闭集模板（垃圾邮件指纹、编码主题行、部门前缀）
     命中即返回。它们覆盖了官方语料 520/520，且逐字可复核。
  2. **语义兜底交给 flash 档** —— 规则无法确定时才调用 Gemini，
     强制对齐 `ClassifyOut` JSON Schema，并要求给出逐字证据片段。
     规则层与模型层不是竞争关系，而是"置信度阶梯"。
"""
from __future__ import annotations

from ..pipeline.classify import (
    ClassificationResult, attachment_expectation, classify_email, intent_flags,
    rule_classify, strip_boilerplate,
)
from .base import ROLE_TRIAGE, Agent, AgentStep, PipelineState


class TriageAgent(Agent):
    name = "triage"
    role = ROLE_TRIAGE
    description = ("Mail routing: closed-set expert rules first (verbatim reviewable), with a "
                   "flash-tier model behind a response_schema contract as the fallback")

    async def execute(self, state: PipelineState) -> AgentStep:
        email = state.email
        ruled = rule_classify(email)

        if ruled is not None:
            state.classification = ruled
            return AgentStep(
                agent=self.name, role=self.role, used_llm=False,
                summary=f"Expert rule matched → {ruled.category} ({ruled.rule_name})",
                evidence={"category": ruled.category, "rule": ruled.rule_name,
                          "confidence": round(ruled.confidence, 3),
                          "evidence_span": ruled.evidence_span})

        if state.use_llm and state.client is not None:
            result = await classify_email(email, client=state.client,
                                          use_rules=True, use_llm=True)
            state.classification = result
            return AgentStep(
                agent=self.name, role=self.role,
                used_llm=result.decided_by == "llm", model=result.model,
                summary=f"Model routed → {result.category} (confidence {result.confidence:.2f})",
                error=result.error,
                evidence={"category": result.category, "model": result.model,
                          "evidence_span": result.evidence_span,
                          "latency_ms": result.latency_ms})

        # 无规则命中且不允许 LLM：保守落到 GENERAL（非对照类）并显式标注
        flags = intent_flags(strip_boilerplate(email.body))
        state.classification = ClassificationResult(
            email_id=email.email_id, category="GENERAL", confidence=0.3,
            decided_by="rule", evidence_span="(no rule matched and the model channel is off)",
            attachment_expectation=attachment_expectation(email), intent=flags,
            rule_name="fallback:general", error="llm_disabled")
        return AgentStep(
            agent=self.name, role=self.role, used_llm=False,
            summary="No rule matched and the model channel is off → conservatively routed as GENERAL",
            error="llm_disabled",
            evidence={"category": "GENERAL", "attachments": email.attachment_count})
