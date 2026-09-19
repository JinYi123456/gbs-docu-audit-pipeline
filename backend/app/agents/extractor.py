"""Extractor Agent —— 多模态感知层。唯一"看得见图"的角色。

职责边界（越界就会毁掉可复现性）：
  · 只做**抽取**：把 SI / BL 各自的 7 个 canonical 字段读出来，附带
    逐字段置信度、原文证据、空白字段清单；
  · **不做判定**：它不知道也不关心两侧是否一致 —— 那是 CrossVerifier 的事。

两条通道产出**同一个 `ExtractionResult` 结构**，因此下游完全无感：
  · 多模态通道（主推）：PDF 直接以原生字节喂给 gemini（保留表格/两栏布局），
    其余格式走文本通道以省 token 并可缓存；
  · 规则通道（兜底）：标签映射 + 硬归一，无 API Key / 断网 / 限流时无缝接管。

这种"双通道同构"是本项目的核心工程手法：**AI 是增强项，不是单点依赖**。
演示现场网络抖动、配额耗尽、模型下线，链路都还能出结果（只是置信度口径不同，
并在 step 里如实标注）。
"""
from __future__ import annotations

from ..pipeline.extract import (
    ROLE_BL, ROLE_SI, DocSlot, ExtractionResult, extract_document_pair, load_slots,
)
from ..pipeline.normalize import COMPARE_FIELDS
from .base import ROLE_EXTRACTOR, Agent, AgentStep, PipelineState


# Machine mode ids → narrative labels for the trace panel.
_MODE_LABELS = {"PAIR": "PAIR", "SINGLE": "SINGLE", "RULE": "Expert-rule"}


class ExtractorAgent(Agent):
    name = "extractor"
    role = ROLE_EXTRACTOR
    description = ("Multimodal extraction of the 7 canonical fields: PDFs go in as native bytes "
                   "(layout preserved), other formats via the text channel; failures degrade to "
                   "the rule channel with an identical output shape")

    # 允许外部预置槽位（上传场景由内存字节构造，跑批场景由附件路径构造）
    def __init__(self, si_slot: DocSlot | None = None, bl_slot: DocSlot | None = None,
                 *, slots_provided: bool = False) -> None:
        self._si_slot = si_slot
        self._bl_slot = bl_slot
        self._slots_provided = slots_provided

    def _resolve_slots(self, state: PipelineState) -> tuple[DocSlot | None, DocSlot | None]:
        if self._slots_provided:
            return self._si_slot, self._bl_slot
        if state.source is None:
            return None, None
        return load_slots(state.source, state.email.attachments)

    async def execute(self, state: PipelineState) -> AgentStep:
        si_slot, bl_slot = self._resolve_slots(state)
        if si_slot is None and bl_slot is None:
            return AgentStep(agent=self.name, role=self.role,
                             summary="No comparable attachment (both sides absent) — extraction skipped",
                             evidence={"si": False, "bl": False})

        result = await extract_document_pair(
            state.email.email_id, si_slot, bl_slot, client=state.client,
            use_llm=state.use_llm and state.client is not None)
        state.extraction = result

        populated = {
            side: sum(1 for value in (view.values or {}).values() if value not in (None, ""))
            for side, view in (("si", result.si), ("bl", result.bl)) if view is not None
        }
        native_modal = [
            slot.doc_type for slot in (si_slot, bl_slot)
            if slot is not None and slot.document.extension == ".pdf"
            and slot.document.raw_bytes and slot.document.is_readable
        ]
        llm_used = bool(result.model) and not str(result.model).startswith("rule")
        return AgentStep(
            agent=self.name, role=self.role, used_llm=llm_used, model=result.model or None,
            summary=(f"{_MODE_LABELS.get(result.mode, result.mode)} extraction: "
                     f"SI {populated.get('si', 0)}/7 · BL {populated.get('bl', 0)}/7"
                     + (" (native multimodal)" if native_modal else " (expert-rule channel)")),
            error=result.error,
            evidence={
                "mode": result.mode, "model": result.model,
                "native_modal_sides": native_modal,
                "fields_populated": populated,
                "from_cache": result.from_cache, "latency_ms": result.latency_ms,
                "canonical_fields": list(COMPARE_FIELDS),
                "readable": {
                    "si": bool(result.si and result.si.is_readable),
                    "bl": bool(result.bl and result.bl.is_readable),
                },
            })


def slots_for_paths(source: object, attachments: tuple[str, ...]) -> tuple[DocSlot | None, DocSlot | None]:
    """给 CLI/测试用的小工具：按官方命名约定分槽。"""
    from ..pipeline.extract import split_attachments
    from ..ingest.readers import read_document

    si_path, bl_path = split_attachments(attachments)
    si_slot = DocSlot(ROLE_SI, read_document(source, si_path)) if si_path else None
    bl_slot = DocSlot(ROLE_BL, read_document(source, bl_path)) if bl_path else None
    return si_slot, bl_slot


__all__ = ["ExtractorAgent", "slots_for_paths", "ExtractionResult"]
