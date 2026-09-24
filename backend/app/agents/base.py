"""Agent 基座：状态容器 + 可审计执行步骤。

只做三件事，刻意不引入任何框架（hackathon 里框架是负债不是资产）：
  1. `PipelineState` —— 四个 Agent 传递的**唯一**共享状态（显式、无隐藏全局）
  2. `AgentStep`     —— 每段计算的可审计记录（谁做的、多久、用了什么、结论是什么）
  3. `Agent`         —— 统一执行协议（自动计时 + 异常隔离 + 轨迹落盘）

异常隔离的取舍：单个 Agent 失败**不中断整条链**，而是把错误写进自己的 step，
并让下游按"证据不足"处理（例如抽取失败 → 裁决层给 NEEDS_REVIEW）。
这与 runner 的单封失败隔离是同一条工程原则：绝不因为一份坏文件丢掉整批结果。
"""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any

from ..ingest.inbox import InboxEmail
from ..pipeline.classify import ClassificationResult
from ..pipeline.compare import ComparisonReport
from ..pipeline.extract import ExtractionResult
from ..pipeline.policy import PolicyOutcome

ROLE_TRIAGE = "triage"
ROLE_EXTRACTOR = "extractor"
ROLE_VERIFIER = "verifier"
ROLE_JUDGE = "judge"


@dataclass(slots=True)
class AgentStep:
    """一个 Agent 的一次执行记录（会随结果落库，前端与复盘都读它）。"""

    agent: str
    role: str
    duration_ms: int = 0
    summary: str = ""
    used_llm: bool = False
    model: str | None = None
    error: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent, "role": self.role,
            "duration_ms": self.duration_ms, "summary": self.summary,
            "used_llm": self.used_llm, "model": self.model, "error": self.error,
            "evidence": self.evidence,
        }


@dataclass(slots=True)
class PipelineState:
    """四个 Agent 之间的共享状态。显式字段 = 谁读谁写一目了然。"""

    email: InboxEmail
    source: Any | None = None
    client: Any | None = None
    use_llm: bool = False
    trust_user_intent: bool = False      # True = 用户主动上传，按"就是要比对"处理

    classification: ClassificationResult | None = None
    extraction: ExtractionResult | None = None
    report: ComparisonReport | None = None
    outcome: PolicyOutcome | None = None

    steps: list[AgentStep] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    # -- 便捷视图 -----------------------------------------------------------
    @property
    def si_view(self) -> Any:
        return self.extraction.si if self.extraction else None

    @property
    def bl_view(self) -> Any:
        return self.extraction.bl if self.extraction else None

    @property
    def documents_present(self) -> bool:
        return self.si_view is not None and self.bl_view is not None

    def trace(self) -> list[dict[str, Any]]:
        return [step.as_dict() for step in self.steps]

    def trace_summary(self) -> dict[str, Any]:
        return {
            "steps": self.trace(),
            "total_ms": sum(step.duration_ms for step in self.steps),
            "llm_calls": sum(1 for step in self.steps if step.used_llm),
            "agents": [step.agent for step in self.steps],
            "errors": [f"{step.agent}: {step.error}" for step in self.steps if step.error],
        }


class Agent(abc.ABC):
    """所有 Agent 的统一协议。"""

    name: str = "agent"
    role: str = ROLE_TRIAGE
    description: str = ""

    async def execute(self, state: PipelineState) -> AgentStep:
        """子类实现：只写 state，返回值会再被基类补上耗时等信息。"""
        raise NotImplementedError

    async def run(self, state: PipelineState) -> AgentStep:
        started = time.perf_counter()
        error: str | None = None
        try:
            step = await self.execute(state)
        except Exception as exc:      # noqa: BLE001 —— 单 Agent 失败不中断整条链
            step = AgentStep(agent=self.name, role=self.role,
                             summary=f"Agent failed, continuing with insufficient evidence: {type(exc).__name__}")
            error = f"{type(exc).__name__}: {exc}"[:300]
        step.agent = step.agent or self.name
        step.role = step.role or self.role
        step.duration_ms = int((time.perf_counter() - started) * 1000)
        step.error = error or step.error
        state.steps.append(step)
        return step

    def __repr__(self) -> str:      # pragma: no cover
        return f"<{type(self).__name__} name={self.name} role={self.role}>"
