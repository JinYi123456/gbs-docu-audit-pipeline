"""Orchestrator —— 把四个 Agent 串成一条可审计的流水线。

编排规则很朴素，但每条都有理由：

  1. **顺序固定**（triage → extractor → verifier → judge）。
     契约已在四轮迭代中冻结：判定矩阵依赖抽取结果、裁决依赖判定结果。
     任何"让 Agent 自由协商"的设计都会让分数不可复现。

  2. **短路（short-circuit）**：Triage 判定为非对照类邮件时，
     直接跳过后面三个 Agent —— 不为垃圾邮件付多模态抽取的钱。

  3. **降级（degrade）**：任一步失败不中断，后续 Agent 按"证据不足"处理。
     例如 LLM 限流 → 抽取退回规则通道；两侧都不可读 → 裁决给 NEEDS_REVIEW。

  4. **全程留痕**：每个 Agent 产出 `AgentStep`（耗时/模型/结论/证据），
     最终随记录一起落库（`upload_runs.payload.agents` 与 `emails` 的诊断字段），
     前端可展开看"这封结论是怎么一步一步来的"。

入口：
    python -m app.agents.orchestrator --email email_031   # 命令行复盘一封邮件
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Sequence

from ..ingest.inbox import InboxEmail, InboxSource
from ..pipeline.classify import CATEGORY_BL_COMPARISON
from .base import Agent, PipelineState
from .escalation_judge import EscalationJudgeAgent
from .extractor import ExtractorAgent
from .triage import TriageAgent
from .verifier import CrossVerifierAgent


@dataclass(slots=True)
class OrchestratorResult:
    state: PipelineState
    skipped: list[str]

    @property
    def record(self) -> dict[str, Any]:
        outcome = self.state.outcome
        classification = self.state.classification
        return {
            "category": classification.category if classification else "GENERAL",
            "status": outcome.status if outcome else "OK",
            "review_reason": outcome.review_reason if outcome else None,
            "has_defect": bool(outcome.has_defect) if outcome else False,
            "defect_fields": sorted(outcome.defect_fields) if outcome else [],
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "record": self.record,
            "trace": self.state.trace_summary(),
            "notes": self.state.notes,
            "skipped_agents": self.skipped,
        }


class VerificationOrchestrator:
    def __init__(self, agents: Sequence[Agent]) -> None:
        if not agents:
            raise ValueError("至少需要一个 Agent")
        self.agents = list(agents)

    async def run(self, state: PipelineState) -> OrchestratorResult:
        skipped: list[str] = []
        for index, agent in enumerate(self.agents):
            # 短路：非对照类邮件无需抽取/比对/升级
            if index > 0 and state.classification is not None \
                    and state.classification.category != CATEGORY_BL_COMPARISON:
                skipped.extend(rest.name for rest in self.agents[index:])
                break
            await agent.run(state)
        return OrchestratorResult(state=state, skipped=skipped)

    def describe(self) -> list[dict[str, str]]:
        """给前端/答辩用的编排说明（不含任何业务数据）。"""
        return [{"agent": agent.name, "role": agent.role,
                 "description": agent.description} for agent in self.agents]


async def verify_email(
    email: InboxEmail,
    *,
    source: InboxSource | None = None,
    client: Any = None,
    use_llm: bool = False,
    si_slot: Any = None,
    bl_slot: Any = None,
    slots_provided: bool = False,
    blank_is_defect: bool | None = None,
) -> OrchestratorResult:
    """单封邮件的完整多 Agent 校验（跑批与上传共用）。"""
    state = PipelineState(email=email, source=source, client=client, use_llm=use_llm)

    # 缺一侧时的占位 report：让裁决层按"缺附件 / 不可读"走升级阶梯而不是崩。
    # ★ 可读性必须取自**文档本身**（`is_readable`），而不是"槽位是否存在"：
    #   槽位存在但文件是图片型扫描件同样不可读，两者混同会让升级理由从
    #   `unreadable` 错报成 `missing_attachment`，人审会被指向错误的方向。
    from ..pipeline.compare import REASON_UNREADABLE, ComparisonReport

    def _readable(slot: Any) -> bool:
        document = getattr(slot, "document", None)
        if document is None:
            return False
        return bool(getattr(document, "is_readable", False))

    placeholder = ComparisonReport(
        comparisons=(), defect_fields=(), matched_fields=(), undecided_fields=(),
        status="OK", has_defect=False,
        review_reason=None if (_readable(si_slot) or _readable(bl_slot)) else REASON_UNREADABLE,
        escalation_signals={"si_readable": _readable(si_slot),
                            "bl_readable": _readable(bl_slot)})

    orchestrator = VerificationOrchestrator([
        TriageAgent(),
        ExtractorAgent(si_slot, bl_slot, slots_provided=slots_provided),
        CrossVerifierAgent(blank_is_defect=blank_is_defect),
        EscalationJudgeAgent(empty_report=placeholder),
    ])
    return await orchestrator.run(state)


# ---------------------------------------------------------------------------
# CLI 复盘：打印一封邮件的完整 Agent 轨迹
# ---------------------------------------------------------------------------
def _main(argv: Sequence[str] | None = None) -> int:      # pragma: no cover
    import argparse
    import sys

    from ..config import gemini_api_key_available, resolve_data_dir
    from ..console import harden_console

    harden_console()
    parser = argparse.ArgumentParser(description="多 Agent 轨迹复盘（只看，不写库）")
    # 注意：--email 不能设 required=True —— 否则 `--describe` 单独使用会被 argparse
    # 直接拒掉（实测踩过），而 describe 恰恰是答辩时最常用的那一个。
    parser.add_argument("--email", default="", help="email_id，例如 email_031")
    parser.add_argument("--no-llm", action="store_true", help="强制确定性通道")
    parser.add_argument("--describe", action="store_true", help="只打印编排说明")
    args = parser.parse_args(argv)
    if not args.email and not args.describe:
        parser.error("需要 --email（或用 --describe 看编排说明）")

    from . import default_orchestrator

    if args.describe:
        for item in default_orchestrator().describe():
            print(f"{item['role']:<12} {item['agent']:<18} {item['description']}")
        return 0

    source = InboxSource("data", data_dir=resolve_data_dir("data"))
    email = next((mail for mail in source.emails() if mail.email_id == args.email), None)
    if email is None:
        print(f"找不到 {args.email}")
        return 2

    client = None
    if not args.no_llm and gemini_api_key_available():
        from ..llm.gemini import GeminiSettings, get_client
        client = get_client(GeminiSettings.from_env())

    result = asyncio.run(verify_email(
        email, source=source, client=client,
        use_llm=client is not None))
    payload = result.as_dict()
    print(json.dumps(payload["record"], ensure_ascii=False, indent=2))
    print(f"\n执行轨迹（{len(payload['trace']['steps'])} 步，"
          f"{payload['trace']['total_ms']}ms，LLM 调用 {payload['trace']['llm_calls']} 次）：")
    for step in payload["trace"]["steps"]:
        mark = "LLM " if step["used_llm"] else "规则"
        print(f"  [{mark}] {step['agent']:<18} {step['duration_ms']:>6}ms  {step['summary']}")
    if payload["skipped_agents"]:
        print(f"  短路跳过：{', '.join(payload['skipped_agents'])}")
    return 0


if __name__ == "__main__":      # pragma: no cover
    import sys

    sys.exit(_main())
