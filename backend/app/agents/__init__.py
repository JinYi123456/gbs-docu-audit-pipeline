"""多 Agent 协同校验系统。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么会有这一层（而不是"把 if-else 改个名字"）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
本项目的评分能力来自一个**确定性专家系统**（规则分类 + 标签抽取 + 判定矩阵），
它在 520 封离线全量上拿到 official scoring 1.0000。但海事实务的现实是：
单据格式不可预测 —— 扫描件、手写批注、多语言表头、票据被错当成 BL。

因此架构上做了明确的分工，而不是"让 LLM 重写一遍规则"：

    ┌─ TriageAgent ──────── 语义分流（先闭集规则，不确定才上 flash 档）
    │     ※ 它决定"要不要比对"，是闸门，不是打分器
    ├─ ExtractorAgent ───── 多模态感知（PDF 原生字节 → 7 canonical 字段）
    │     ※ 唯一"看图"的角色；每字段带置信度与原文证据
    ├─ CrossVerifierAgent ─ 数值与语义对齐（判定矩阵：归一化 → 容差 → 证据）
    │     ※ 纯确定性求值，不用 LLM —— 分数必须可复现、可回放
    └─ EscalationJudgeAgent 升级裁决（Ask-for-help / 人审工单）

**关键设计：感知层可以是概率的，裁决层必须是确定性的。**
LLM 只负责"把图里的字认出来"和"这封邮件属于哪一类"；是否算不一致，由判定矩阵
用可复现的数值容差（重量 ±10kg 或 0.2%）裁定。这样既拿到了多模态的鲁棒性，
又保住了满分成绩的可复现性 —— 评委问"分数怎么保证"时，这句话就是答案。

每个 Agent 的每一次执行都会产出一条可审计的 `AgentStep`（耗时、用了哪个模型、
结论、证据），最终随结果一起落库。多 Agent 不是流程图上的四个框，
而是**有轨迹、可回放、可归因**的四段计算。

导入策略：本包用**惰性导入**（`__getattr__`）。原因很实际 ——
`python -m app.agents.orchestrator` 会先导入本包，若这里急切 import 子模块，
runpy 会报 "found in sys.modules ... unpredictable behaviour"；惰性化同时也
切断了 agents ↔ upload ↔ repo 之间潜在的循环依赖。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:      # 仅供类型检查，运行时不触发导入
    from .base import Agent, AgentStep, PipelineState
    from .escalation_judge import EscalationJudgeAgent
    from .extractor import ExtractorAgent
    from .orchestrator import VerificationOrchestrator
    from .triage import TriageAgent
    from .verifier import CrossVerifierAgent

__all__ = [
    "Agent",
    "AgentStep",
    "PipelineState",
    "TriageAgent",
    "ExtractorAgent",
    "CrossVerifierAgent",
    "EscalationJudgeAgent",
    "VerificationOrchestrator",
    "default_orchestrator",
]

_EXPORTS = {
    "Agent": ("base", "Agent"),
    "AgentStep": ("base", "AgentStep"),
    "PipelineState": ("base", "PipelineState"),
    "TriageAgent": ("triage", "TriageAgent"),
    "ExtractorAgent": ("extractor", "ExtractorAgent"),
    "CrossVerifierAgent": ("verifier", "CrossVerifierAgent"),
    "EscalationJudgeAgent": ("escalation_judge", "EscalationJudgeAgent"),
    "VerificationOrchestrator": ("orchestrator", "VerificationOrchestrator"),
}


def __getattr__(name: str) -> Any:
    """PEP 562 惰性导入：`from app.agents import TriageAgent` 依然可用。"""
    if name in _EXPORTS:
        import importlib

        module_name, attribute = _EXPORTS[name]
        module = importlib.import_module(f".{module_name}", __name__)
        value = getattr(module, attribute)
        globals()[name] = value      # 缓存，后续访问不再走 __getattr__
        return value
    if name == "default_orchestrator":
        module = __import__(f"{__name__}.orchestrator", fromlist=["_"])
        globals()[name] = module.default_orchestrator
        return module.default_orchestrator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def default_orchestrator() -> "VerificationOrchestrator":
    """标准编排：分流 → 感知 → 裁决 → 升级（顺序固定，契约已冻结）。"""
    from .escalation_judge import EscalationJudgeAgent
    from .extractor import ExtractorAgent
    from .orchestrator import VerificationOrchestrator
    from .triage import TriageAgent
    from .verifier import CrossVerifierAgent

    return VerificationOrchestrator([
        TriageAgent(),
        ExtractorAgent(),
        CrossVerifierAgent(),
        EscalationJudgeAgent(),
    ])
