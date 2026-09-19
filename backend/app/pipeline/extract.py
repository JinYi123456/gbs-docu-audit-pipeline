"""STEP 2 —— 核心提取。

两条通道，同一个出口（compare.DocumentView）：
  · LLM 通道：gemini-2.5-pro 多模态（PDF 直接喂原生字节），PAIR / SINGLE 双模式
  · 规则通道：rule_extract 的标签映射 + 硬归一，无 API Key 也能跑通

★ 关键设计：两条通道产出的是**同一种 DocumentView**，
  归一层与比对层完全不需要知道 LLM 的存在 —— 这既是解耦，
  也让两条通道可以互为交叉校验（不一致时把该字段降级人审）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..ingest.inbox import InboxSource
from ..ingest.readers import DocumentText, read_document
from .compare import DocumentView
from .rule_extract import extract_document_view

logger = logging.getLogger("sdoc.extract")

ROLE_SI: str = "SI"
ROLE_BL: str = "BL"

# 原生多模态只对 PDF 开启（保留表格与两栏布局）；文本类走文本通道更省 token 且可缓存
NATIVE_MODAL_EXTENSIONS: frozenset[str] = frozenset({".pdf"})


@dataclass(slots=True, frozen=True)
class DocSlot:
    """一个槽位：我们把它当作 SI 还是 BL，以及它的原始文档。"""

    doc_type: str
    document: DocumentText

    @property
    def path(self) -> str:
        return self.document.path


@dataclass(slots=True)
class ExtractionResult:
    email_id: str
    mode: str                                     # "PAIR" | "SINGLE" | "RULE"
    si: DocumentView | None = None
    bl: DocumentView | None = None
    field_confidence: dict[str, dict[str, float]] = field(default_factory=dict)
    codes: dict[str, dict[str, str]] = field(default_factory=dict)
    evidence: dict[str, dict[str, str]] = field(default_factory=dict)
    model: str = ""
    prompt_version: str = ""
    latency_ms: int = 0
    from_cache: bool = False
    error: str | None = None

    def view(self, doc_type: str) -> DocumentView | None:
        return self.si if doc_type == ROLE_SI else self.bl


# ---------------------------------------------------------------------------
# 附件 → 槽位
# ---------------------------------------------------------------------------
def split_attachments(attachments: Sequence[str]) -> tuple[str | None, str | None]:
    """按文件名后缀 _SI / _BL 分槽（官方命名约定）；分不出时按顺序兜底。"""
    si_path = bl_path = None
    for path in attachments:
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].upper()
        if stem.endswith("_SI") and si_path is None:
            si_path = path
        elif stem.endswith("_BL") and bl_path is None:
            bl_path = path
    leftovers = [path for path in attachments if path not in {si_path, bl_path}]
    if si_path is None and leftovers:
        si_path = leftovers.pop(0)
    if bl_path is None and leftovers:
        bl_path = leftovers.pop(0)
    return si_path, bl_path


def load_slots(source: InboxSource, attachments: Sequence[str]) -> tuple[DocSlot | None, DocSlot | None]:
    si_path, bl_path = split_attachments(attachments)
    si_slot = DocSlot(ROLE_SI, read_document(source, si_path)) if si_path else None
    bl_slot = DocSlot(ROLE_BL, read_document(source, bl_path)) if bl_path else None
    return si_slot, bl_slot


# ---------------------------------------------------------------------------
# 通道分派
# ---------------------------------------------------------------------------
async def extract_document_pair(
    email_id: str,
    si_slot: DocSlot | None,
    bl_slot: DocSlot | None,
    *,
    client: Any | None = None,
    use_llm: bool = True,
) -> ExtractionResult:
    """统一入口：优先 LLM，不可用或未启用则走规则通道。"""
    if not use_llm or client is None:
        return extract_rule(email_id, si_slot, bl_slot)
    try:
        return await extract_pair(email_id, si_slot, bl_slot, client=client)
    except Exception as exc:      # noqa: BLE001 —— LLM 通道整体失败时退回规则通道
        logger.warning("LLM 提取通道失败，退回规则通道：%s", exc)
        result = extract_rule(email_id, si_slot, bl_slot)
        result.error = f"llm_failed_then_rule: {type(exc).__name__}: {exc}"[:300]
        return result


def extract_rule(
    email_id: str,
    si_slot: DocSlot | None,
    bl_slot: DocSlot | None,
) -> ExtractionResult:
    """规则通道：标签映射 + 硬归一。无 API Key 时的主力通道。"""
    result = ExtractionResult(email_id=email_id, mode="RULE", model="rule-extractor")
    for slot in (si_slot, bl_slot):
        if slot is None:
            continue
        view = extract_document_view(slot.doc_type, slot.document)
        if slot.doc_type == ROLE_SI:
            result.si = view
        else:
            result.bl = view
        key = slot.doc_type.lower()
        result.field_confidence[key] = dict(view.field_confidence)
        result.evidence[key] = {}
    return result


# ---------------------------------------------------------------------------
# LLM 通道
# ---------------------------------------------------------------------------
def content_parts(slot: DocSlot, types: Any) -> list[Any]:
    """PDF 走原生多模态（保留表格与列布局），其余走文本通道。"""
    document = slot.document
    if document.extension in NATIVE_MODAL_EXTENSIONS and document.raw_bytes \
            and document.is_readable:
        return [types.Part.from_bytes(data=document.raw_bytes, mime_type=document.mime)]
    return [f"===== {slot.doc_type} ({slot.path}) =====\n{document.text}"]


def _doc_instruction(slot: DocSlot | None) -> str:
    if slot is None:
        return "(未提供)"
    status = "可读" if slot.document.is_readable else f"不可读（{slot.document.read_error}）"
    return f"path={slot.path} mime={slot.document.mime} readable={status}"


async def extract_pair(
    email_id: str,
    si_slot: DocSlot | None,
    bl_slot: DocSlot | None,
    *,
    client: Any,
) -> ExtractionResult:
    """SI+BL 齐备时一次调用抽出两侧 7 字段；任一侧不可用则降级为 SINGLE。"""
    from ..llm.gemini import GeminiError
    from ..llm.prompts import extract_prompt, versions
    from ..llm.schemas import ExtractOut

    started = time.perf_counter()
    result = ExtractionResult(
        email_id=email_id, mode="PAIR", model=client.settings.model_extract,
        prompt_version=versions()["extract"])

    if si_slot and bl_slot and si_slot.document.is_readable and bl_slot.document.is_readable:
        try:
            _, types = client.sdk()
            contents: list[Any] = []
            contents.extend(content_parts(si_slot, types))
            contents.extend(content_parts(bl_slot, types))
            contents.append(
                "EXTRACT now. Return the JSON object with exactly the keys defined in the "
                "system instruction: si, bl, doc_roles, other_doc_kind, field_confidence, "
                "blank_fields, codes, evidence.")
            parsed, from_cache = await client.generate_structured(
                schema=ExtractOut, contents=contents,
                system_instruction=extract_prompt(
                    "PAIR",
                    f"[{_doc_instruction(si_slot)}]\n{si_slot.document.text}",
                    f"[{_doc_instruction(bl_slot)}]\n{bl_slot.document.text}"),
                model=client.settings.model_extract,
                task="extract",
                thinking_budget=client.settings.extract_thinking_budget,
                cache_payload=[si_slot.document.text_sha256 or si_slot.path,
                               bl_slot.document.text_sha256 or bl_slot.path])
            result.si = DocumentView(
                doc_type=ROLE_SI, values=_values(parsed.si),
                blank_fields=frozenset(parsed.blank_fields.si),
                doc_role=parsed.doc_roles.a, other_doc_kind=parsed.other_doc_kind.a,
                is_readable=True,
                field_confidence=parsed.field_confidence.si.model_dump(),
                source_path=si_slot.path, text_sha256=si_slot.document.text_sha256,
                extractor="llm")
            result.bl = DocumentView(
                doc_type=ROLE_BL, values=_values(parsed.bl),
                blank_fields=frozenset(parsed.blank_fields.bl),
                doc_role=parsed.doc_roles.b, other_doc_kind=parsed.other_doc_kind.b,
                is_readable=True,
                field_confidence=parsed.field_confidence.bl.model_dump(),
                source_path=bl_slot.path, text_sha256=bl_slot.document.text_sha256,
                extractor="llm")
            result.field_confidence = {"si": parsed.field_confidence.si.model_dump(),
                                       "bl": parsed.field_confidence.bl.model_dump()}
            result.codes = {"si": parsed.codes.si.model_dump(),
                            "bl": parsed.codes.bl.model_dump()}
            result.evidence = {"si": parsed.evidence.si.model_dump(),
                               "bl": parsed.evidence.bl.model_dump()}
            result.from_cache = from_cache
            result.latency_ms = int((time.perf_counter() - started) * 1000)
            return result
        except (GeminiError, Exception) as exc:      # noqa: BLE001
            logger.warning("PAIR 提取失败，降级为 SINGLE：%s", exc)
            result.error = f"pair_failed: {type(exc).__name__}"

    slots = [slot for slot in (si_slot, bl_slot) if slot is not None]
    singles = await asyncio.gather(
        *(extract_single(email_id, slot, client=client) for slot in slots))
    for slot, single in zip(slots, singles):
        if slot.doc_type == ROLE_SI:
            result.si = single.si
        else:
            result.bl = single.bl
        key = slot.doc_type.lower()
        result.field_confidence[key] = single.field_confidence.get(key, {})
        result.codes[key] = single.codes.get(key, {})
        result.evidence[key] = single.evidence.get(key, {})
        if single.error:
            result.error = single.error
    result.latency_ms = int((time.perf_counter() - started) * 1000)
    return result


async def extract_single(email_id: str, slot: DocSlot, *, client: Any) -> ExtractionResult:
    """MODE=SINGLE：单文档抽取（含文档角色实判）。"""
    from ..llm.gemini import GeminiError
    from ..llm.prompts import extract_prompt, versions
    from ..llm.schemas import ExtractSingleOut

    started = time.perf_counter()
    key = slot.doc_type.lower()
    result = ExtractionResult(email_id=email_id, mode="SINGLE",
                              model=client.settings.model_extract,
                              prompt_version=versions()["extract"])

    if not slot.document.is_readable:
        setattr(result, "si" if slot.doc_type == ROLE_SI else "bl",
                _empty_view(slot, error=slot.document.read_error or "unreadable"))
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    try:
        _, types = client.sdk()
        contents = list(content_parts(slot, types))
        contents.append("EXTRACT now. Return exactly the keys defined in the system instruction.")
        parsed, from_cache = await client.generate_structured(
            schema=ExtractSingleOut, contents=contents,
            system_instruction=extract_prompt(
                "SINGLE", f"[{_doc_instruction(slot)}]\n{slot.document.text}", ""),
            model=client.settings.model_extract,
            task="extract",
            thinking_budget=client.settings.extract_thinking_budget,
            cache_payload=[slot.doc_type, slot.document.text_sha256 or slot.path])
        view = DocumentView(
            doc_type=slot.doc_type, values=_values(parsed.doc),
            blank_fields=frozenset(parsed.blank_fields), doc_role=parsed.doc_role,
            other_doc_kind=parsed.other_doc_kind, is_readable=True,
            field_confidence=parsed.field_confidence.model_dump(),
            source_path=slot.path, text_sha256=slot.document.text_sha256, extractor="llm")
        setattr(result, "si" if slot.doc_type == ROLE_SI else "bl", view)
        result.field_confidence[key] = parsed.field_confidence.model_dump()
        result.codes[key] = parsed.codes.model_dump()
        result.evidence[key] = parsed.evidence.model_dump()
        result.from_cache = from_cache
    except (GeminiError, Exception) as exc:      # noqa: BLE001
        setattr(result, "si" if slot.doc_type == ROLE_SI else "bl",
                _empty_view(slot, error=f"{type(exc).__name__}: {exc}"[:300]))
        result.error = f"{type(exc).__name__}: {exc}"[:300]

    result.latency_ms = int((time.perf_counter() - started) * 1000)
    return result


def _values(values: Any) -> dict[str, Any]:
    from ..llm.schemas import CANONICAL_FIELDS
    return {name: getattr(values, name) for name in CANONICAL_FIELDS}


def _empty_view(slot: DocSlot, *, error: str) -> DocumentView:
    from .normalize import COMPARE_FIELDS
    return DocumentView(
        doc_type=slot.doc_type, values={name: None for name in COMPARE_FIELDS},
        blank_fields=frozenset(), doc_role="UNKNOWN", is_readable=False,
        read_error=error, source_path=slot.path)
