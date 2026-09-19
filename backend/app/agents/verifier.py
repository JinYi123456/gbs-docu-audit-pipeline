"""Cross-Verifier Agent —— 数值与语义对齐。整条链里**唯一**判定"是否不一致"的角色。

★ 这个 Agent 刻意**不用 LLM**，这是设计决策而不是省事：

  1. 官方 end_to_end 指标要求 defect_fields **集合完全相等**（多一个也算错），
     而 LLM 输出天然带随温度波动的概率性 —— 让它直接产出缺陷集合，
     就等于把 50% 的分数押在不可复现的采样上。
  2. 分数必须可回放：同一个输入跑两次必须给出同一份结论，评委追问"怎么保证"
     时，答案是"裁决层没有随机性，只有显式容差"。
  3. 数值对齐本来就有客观判据（重量 ±10kg 或 0.2%），不需要"智能"。

于是职责切成两半：
    · Extractor（概率）：把图里的字认出来，给出原文与置信度；
    · CrossVerifier（确定）：归一 → 精确/别名/模糊/数值容差 → 逐字段结论。

它执行的是本项目的**判定矩阵**：
    文本字段：实体大写去噪 → 别名归一 → 相似度 ≥ 0.94 判一致
    港口字段：拆出城市/国家成分 → UN/LOCODE 与国名护栏 → 双向包含比对
    数量字段：取前导整型（`11 x 40'HC` → 11）
    重量字段：统一换算成 KG → 容差 ±10kg 或 ±0.2%（取更宽者）
    单侧空白：孤立的空白 = 不确定（进人审）；有确证缺陷旁的空白 = 实质不一致
"""
from __future__ import annotations

from ..pipeline.compare import (
    ComparisonReport, compare_documents,
)
from .base import ROLE_VERIFIER, Agent, AgentStep, PipelineState


class CrossVerifierAgent(Agent):
    name = "cross_verifier"
    role = ROLE_VERIFIER
    description = ("Deterministic judgement matrix: normalise → exact / alias / fuzzy / numeric "
                   "tolerance → per-field verdict; one-sided blanks escalate only with "
                   "corroborating evidence, so every verdict is reproducible")

    def __init__(self, *, blank_is_defect: bool | None = None) -> None:
        # None = 由正文证据决定（与 runner 同一条判据）；显式布尔值用于测试/上传
        self._blank_is_defect = blank_is_defect

    async def execute(self, state: PipelineState) -> AgentStep:
        si_view, bl_view = state.si_view, state.bl_view
        if si_view is None or bl_view is None:
            return AgentStep(
                agent=self.name, role=self.role, used_llm=False,
                summary="One side missing — the escalation ladder decides",
                evidence={"si_present": si_view is not None, "bl_present": bl_view is not None})

        from ..pipeline.compare import REASON_MISSING_VALUE

        body_hint = state.classification.intent.doc_issue_hint if state.classification else None
        blank_is_defect = (self._blank_is_defect if self._blank_is_defect is not None
                           else body_hint != REASON_MISSING_VALUE)
        report: ComparisonReport = compare_documents(
            si_view, bl_view, blank_is_defect=blank_is_defect)
        state.report = report
        state.notes["blank_is_defect"] = blank_is_defect

        # 注意属性名是 `field`（入 db 行时才映射成 `field_name`）——
        # 早先写成 row.field_name，导致本 Agent 静默抛 AttributeError，
        # 靠 judge 的占位 report 兜住，结论看起来"对"但轨迹里少了一次判定。
        methods: dict[str, str] = {}
        for row in report.comparisons:
            methods[row.field] = row.match_method
        return AgentStep(
            agent=self.name, role=self.role, used_llm=False,
            summary=(f"Judgement matrix: {len(report.defect_fields)} mismatched · "
                     f"{len(report.matched_fields)} matched · "
                     f"{len(report.undecided_fields)} undecided"),
            evidence={
                "blank_is_defect": blank_is_defect,
                "defect_fields": sorted(report.defect_fields),
                "matched_fields": sorted(report.matched_fields),
                "undecided_fields": sorted(report.undecided_fields),
                "match_methods": methods,
                "tolerances": {
                    "text_similarity_threshold": 0.94,
                    "weight_abs_tolerance_kg": 10,
                    "weight_rel_tolerance_pct": 0.2,
                },
            })
