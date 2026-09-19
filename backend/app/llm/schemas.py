"""Gemini 结构化输出 Schema。

★★ 铁律：凡是喂给 response_schema 的模型，一律**不许有默认值**。

本机实测（google-genai 2.22.0 / pydantic 2.13.5）：
    hint: Optional[X] = None  → model_json_schema() 的 required 里没有 hint，
                                且 schema 里出现 "default" 键
    hint: Optional[X]         → required 里包含 hint，schema 无 "default"
带默认值的 schema 会在**请求时**抛 `ValueError: Default value is not supported`；
GenerateContentConfig 构造时不校验，所以这个坑只会在跑批中途爆出来。
因此这里全部字段都是「必填但可空」，宽松化交给本地校验器完成。

顺带一个必须记住的坑：2.5-pro 的思考 token 计入 max_output_tokens，
给少了 JSON 会被截断 —— 这就是 GEMINI_MAX_OUTPUT_TOKENS 默认 8192 的原因。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

Category = Literal["BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"]
DocRole = Literal["SI", "BL", "OTHER", "UNKNOWN"]
OtherDocKind = Literal["commercial_invoice", "packing_list", "coo"]
ReviewReason = Literal["wrong_doc_type", "missing_attachment", "unreadable", "missing_value"]
FieldName = Literal[
    "shipper", "consignee", "notify_party", "port_of_loading",
    "port_of_discharge", "container_count", "gross_weight_kg",
]

CANONICAL_FIELDS: tuple[str, ...] = (
    "shipper", "consignee", "notify_party", "port_of_loading",
    "port_of_discharge", "container_count", "gross_weight_kg",
)


# ---------------------------------------------------------------------------
# 本地宽松化（不参与 response_schema 的字段约束，可自由带默认值）
# ---------------------------------------------------------------------------
def _coerce_count(value: Any) -> Any:
    if value is None or isinstance(value, int):
        return value
    from ..pipeline.normalize import norm_count
    return norm_count(value)


def _coerce_weight(value: Any) -> Any:
    if value is None or isinstance(value, (int, float)):
        return value
    from ..pipeline.normalize import norm_weight
    kilograms = norm_weight(value)
    return None if kilograms is None else float(kilograms)


class DocValues(BaseModel):
    """7 个 Canonical 字段。可空但必填；数值字段带本地强归一兜底。"""

    shipper: Optional[str]
    consignee: Optional[str]
    notify_party: Optional[str]
    port_of_loading: Optional[str]
    port_of_discharge: Optional[str]
    container_count: Optional[int]
    gross_weight_kg: Optional[float]

    _normalize_count = field_validator("container_count", mode="before")(_coerce_count)
    _normalize_weight = field_validator("gross_weight_kg", mode="before")(_coerce_weight)

    @field_validator("shipper", "consignee", "notify_party", "port_of_loading",
                     "port_of_discharge", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        from ..pipeline.normalize import is_blank
        return None if is_blank(value) else value


class DocEvidence(BaseModel):
    shipper: Optional[str]
    consignee: Optional[str]
    notify_party: Optional[str]
    port_of_loading: Optional[str]
    port_of_discharge: Optional[str]
    container_count: Optional[str]
    gross_weight_kg: Optional[str]


class DocConfidence(BaseModel):
    shipper: float = Field(ge=0, le=1)
    consignee: float = Field(ge=0, le=1)
    notify_party: float = Field(ge=0, le=1)
    port_of_loading: float = Field(ge=0, le=1)
    port_of_discharge: float = Field(ge=0, le=1)
    container_count: float = Field(ge=0, le=1)
    gross_weight_kg: float = Field(ge=0, le=1)


class DocCodes(BaseModel):
    port_of_loading: Optional[str]
    port_of_discharge: Optional[str]


class DocRoles(BaseModel):
    a: DocRole
    b: DocRole


class OtherDocKinds(BaseModel):
    a: Optional[OtherDocKind]
    b: Optional[OtherDocKind]


class BlankFields(BaseModel):
    si: list[FieldName]
    bl: list[FieldName]


class ConfidencePair(BaseModel):
    si: DocConfidence
    bl: DocConfidence


class CodesPair(BaseModel):
    si: DocCodes
    bl: DocCodes


class EvidencePair(BaseModel):
    si: DocEvidence
    bl: DocEvidence


class ExtractOut(BaseModel):
    """MODE=PAIR 的响应 Schema：SI / BL 各 7 个字段，不多不少。"""

    si: DocValues
    bl: DocValues
    doc_roles: DocRoles
    other_doc_kind: OtherDocKinds
    field_confidence: ConfidencePair
    blank_fields: BlankFields
    codes: CodesPair
    evidence: EvidencePair


class ExtractSingleOut(BaseModel):
    """MODE=SINGLE 的响应 Schema（单文件 / 角色复核 / 单档重试）。"""

    doc: DocValues
    doc_role: DocRole
    other_doc_kind: Optional[OtherDocKind]
    field_confidence: DocConfidence
    blank_fields: list[FieldName]
    codes: DocCodes
    evidence: DocEvidence


class IntentFlags(BaseModel):
    has_comparison_intent: bool
    asserts_documents_attached: bool
    doc_issue_hint: Optional[ReviewReason]


class ClassifyOut(BaseModel):
    """STEP 1 响应 Schema。"""

    category: Category
    confidence: float = Field(ge=0, le=1)
    decided_by: Literal["rule", "llm"]
    evidence_span: str
    attachment_expectation: Literal["si+bl", "si_only", "none", "unknown"]
    intent_flags: IntentFlags


SCHEMA_REGISTRY: dict[str, type[BaseModel]] = {
    "classify": ClassifyOut,
    "extract_pair": ExtractOut,
    "extract_single": ExtractSingleOut,
}
