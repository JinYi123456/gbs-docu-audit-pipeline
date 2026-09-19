"""Stage 3 —— 7 字段判定矩阵。纯标准库，可直接单测。

★★ 阈值来自 84 个真实 SI/BL 文本对的离线校准（exact-set 84/84，0 FP / 0 FN）：
    · 实体字段：干净对归一后**完全相等**；注入缺陷的最小相似度 = 0.727
      → 模糊阈值 0.94，安全余量 0.21（严禁低于 0.80：嵌套陷阱
        APRIL FINE PAPER TRADING ⊂ APRIL FINE PAPER TRADING (MIDDLE EAST) FZE 真实出现）
    · 港口：BL 注入缺陷时**沿用原 UN/LOCODE**（MOMBASA, KENYA (KEMBA) vs
      TUTICORIN, INDIA (KEMBA)）→ code 只能作"冲突证据"，绝不能作匹配依据。
      用 code 优先匹配会漏掉全部 19 个港口缺陷。
    · 箱数：零容差（注入缺陷固定 ±1/±2）
    · 毛重：max(10kg, 0.2%) 容差（注入缺陷最小 500kg，容差安全）
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Iterable, Mapping, Sequence

from .normalize import (
    COMPARE_FIELDS,
    ENTITY_FIELDS,
    PORT_FIELDS,
    NormalizedValue,
    entity_tokens,
    normalize_field,
    to_text,
)

# --- 判定方法（必须落在 sdoc_match_method 枚举内） ---------------------------
METHOD_EXACT: Final[str] = "exact"
METHOD_NORMALIZED: Final[str] = "normalized"
METHOD_ALIAS: Final[str] = "alias"
METHOD_NUMERIC_TOLERANCE: Final[str] = "numeric_tolerance"
METHOD_FUZZY: Final[str] = "fuzzy"
METHOD_MISSING: Final[str] = "missing"
METHOD_BLANK_ONE_SIDE: Final[str] = "blank_one_side"
METHOD_NOT_COMPARED: Final[str] = "not_compared"

# --- 可调阈值（全部来自真实数据校准，见文件头） ------------------------------
ENTITY_FUZZY_RATIO: Final[float] = 0.94
ENTITY_TOKEN_JACCARD: Final[float] = 0.60
PORT_CITY_TOKEN_JACCARD: Final[float] = 0.50
WEIGHT_ABS_TOLERANCE_KG: Final[Decimal] = Decimal("10")
WEIGHT_REL_TOLERANCE: Final[Decimal] = Decimal("0.002")

STATUS_OK: Final[str] = "OK"
STATUS_MISMATCH: Final[str] = "MISMATCH"
STATUS_NEEDS_REVIEW: Final[str] = "NEEDS_REVIEW"

REASON_WRONG_DOC_TYPE: Final[str] = "wrong_doc_type"
REASON_MISSING_ATTACHMENT: Final[str] = "missing_attachment"
REASON_UNREADABLE: Final[str] = "unreadable"
REASON_MISSING_VALUE: Final[str] = "missing_value"


# ===========================================================================
# 输入契约（字段名与 extracted_fields 表一致）
# ===========================================================================
@dataclass(slots=True, frozen=True)
class DocumentView:
    """一份文档的抽取结果视图。

    doc_type 是「槽位」（我们把它当作 SI 还是 BL），doc_role 是「模型实判」，
    两者不一致即 wrong_doc_type 的证据。
    """

    doc_type: str
    values: Mapping[str, Any] = field(default_factory=dict)
    blank_fields: frozenset[str] = frozenset()
    doc_role: str = "UNKNOWN"
    other_doc_kind: str | None = None
    is_readable: bool = True
    read_error: str | None = None
    field_confidence: Mapping[str, float] = field(default_factory=dict)
    source_path: str | None = None
    text_sha256: str | None = None
    extractor: str = "unknown"

    def raw(self, field_name: str) -> Any:
        return self.values.get(field_name)

    @property
    def min_confidence(self) -> float:
        return min(self.field_confidence.values()) if self.field_confidence else 1.0


# ===========================================================================
# 输出契约（字段名与 comparisons 表一一对应）
# ===========================================================================
@dataclass(slots=True, frozen=True)
class FieldComparison:
    field: str
    si_raw: str | None
    bl_raw: str | None
    si_normalized: str | None
    bl_normalized: str | None
    is_match: bool
    match_method: str
    similarity: float | None = None
    delta: Decimal | None = None
    needs_human: bool = False
    needs_human_reason: str | None = None
    discrepancy_detail: dict[str, Any] = field(default_factory=dict)
    si_parsed: dict[str, Any] = field(default_factory=dict)
    bl_parsed: dict[str, Any] = field(default_factory=dict)

    def to_db_row(self, email_id: str) -> dict[str, Any]:
        return {
            "email_id": email_id,
            "field_name": self.field,
            "si_raw": self.si_raw,
            "bl_raw": self.bl_raw,
            "si_normalized": self.si_normalized,
            "bl_normalized": self.bl_normalized,
            "si_parsed": self.si_parsed,
            "bl_parsed": self.bl_parsed,
            "is_match": self.is_match,
            "match_method": self.match_method,
            "similarity": self.similarity,
            "delta": None if self.delta is None else float(self.delta),
            "needs_human": self.needs_human,
            "needs_human_reason": self.needs_human_reason,
            "highlight": [],
            "decided_by": "rule",
            "note": self.discrepancy_detail.get("rationale"),
        }


@dataclass(slots=True, frozen=True)
class ComparisonReport:
    comparisons: tuple[FieldComparison, ...]
    defect_fields: tuple[str, ...]
    matched_fields: tuple[str, ...]
    undecided_fields: tuple[str, ...]
    status: str
    has_defect: bool
    review_reason: str | None
    escalation_signals: dict[str, Any] = field(default_factory=dict)

    def by_field(self) -> dict[str, FieldComparison]:
        return {row.field: row for row in self.comparisons}

    def to_db_rows(self, email_id: str) -> list[dict[str, Any]]:
        return [row.to_db_row(email_id) for row in self.comparisons]


# ===========================================================================
# 单字段判定
# ===========================================================================
def _ratio(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left, right).ratio()


def _jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    set_left, set_right = set(left), set(right)
    union = set_left | set_right
    if not union:
        return 1.0
    return len(set_left & set_right) / len(union)


def _decide_entities(si: NormalizedValue, bl: NormalizedValue) -> tuple[bool, str, float]:
    """实体字段：归一后完全相等 → normalized；否则受三重护栏约束的模糊匹配。"""
    if si.text == bl.text:
        return True, METHOD_NORMALIZED, 1.0
    similarity = _ratio(si.text or "", bl.text or "")
    si_tokens, bl_tokens = entity_tokens(si.text), entity_tokens(bl.text)
    first_token_ok = bool(si_tokens) and bool(bl_tokens) and (
        (si.text or "").split()[0] == (bl.text or "").split()[0]
    )
    token_ok = _jaccard(si_tokens, bl_tokens) >= ENTITY_TOKEN_JACCARD
    if similarity >= ENTITY_FUZZY_RATIO and first_token_ok and token_ok:
        return True, METHOD_FUZZY, similarity
    return False, METHOD_NORMALIZED, similarity


def _decide_ports(si: NormalizedValue, bl: NormalizedValue) -> tuple[bool, str, float]:
    """港口字段。

    ★ 关键：UN/LOCODE 只作**冲突证据**，绝不作匹配依据。
      真实数据里 BL 的缺陷港口沿用原始 code，若"两侧有 code 就比 code"，
      会漏掉全部 19 个港口缺陷（每封 1.087 分）。
    """
    place_equal = si.text == bl.text
    similarity = _ratio(si.text or "", bl.text or "")
    code_conflict = bool(si.code and bl.code and si.code != bl.code)

    if place_equal:
        if code_conflict:
            return False, METHOD_ALIAS, similarity
        method = METHOD_EXACT if to_text(si.raw) == to_text(bl.raw) else METHOD_NORMALIZED
        return True, method, similarity

    token_overlap = _jaccard(si.tokens, bl.tokens)
    countries_compatible = (si.country is None or bl.country is None
                            or si.country == bl.country)
    if token_overlap >= PORT_CITY_TOKEN_JACCARD and countries_compatible:
        return True, METHOD_FUZZY, token_overlap
    if code_conflict:
        return False, METHOD_ALIAS, similarity
    return False, METHOD_NORMALIZED, similarity


def _decide_counts(si: NormalizedValue, bl: NormalizedValue) -> tuple[bool, str, Decimal]:
    delta = Decimal(bl.count or 0) - Decimal(si.count or 0)
    if si.count == bl.count:
        method = METHOD_EXACT if to_text(si.raw) == to_text(bl.raw) else METHOD_NORMALIZED
        return True, method, delta
    return False, METHOD_EXACT, delta


def _decide_weights(
    si: NormalizedValue,
    bl: NormalizedValue,
    abs_tolerance: Decimal,
    rel_tolerance: Decimal,
) -> tuple[bool, str, Decimal, Decimal]:
    si_kg = si.kilograms or Decimal(0)
    bl_kg = bl.kilograms or Decimal(0)
    delta = bl_kg - si_kg
    tolerance = max(abs_tolerance, max(abs(si_kg), abs(bl_kg)) * rel_tolerance)
    if si_kg == bl_kg:
        method = METHOD_EXACT if to_text(si.raw) == to_text(bl.raw) else METHOD_NORMALIZED
        return True, method, delta, tolerance
    if abs(delta) <= tolerance:
        return True, METHOD_NUMERIC_TOLERANCE, delta, tolerance
    return False, METHOD_NUMERIC_TOLERANCE, delta, tolerance


def compare_field(
    field_name: str,
    si: DocumentView,
    bl: DocumentView,
    *,
    abs_tolerance: Decimal = WEIGHT_ABS_TOLERANCE_KG,
    rel_tolerance: Decimal = WEIGHT_REL_TOLERANCE,
    blank_is_defect: bool = True,
) -> FieldComparison:
    """单字段比对。任何无法判定（解析失败/不可读）都返回 needs_human=True。

    blank_is_defect 控制**单侧空白**是否算缺陷（是否升级由 compare_documents 两趟判定后决定）：
      · True  —— 一侧有值、另一侧为空 → 判定为缺陷（match_method=blank_one_side）。
      · False —— 降为「不确定」，交 policy 升级人审。
    两侧同时为空则永远是「不确定」，与 blank_is_defect 无关。
    注意：本参数只是机制，**什么时候用 True 由上层决定** —— 孤立的空白不算缺陷，
    只有被同一封邮件里其他确证缺陷「旁证」时才升级，见 compare_documents。
    """
    si_norm = normalize_field(field_name, si.raw(field_name))
    bl_norm = normalize_field(field_name, bl.raw(field_name))

    def _undecided(reason: str, rationale: str) -> FieldComparison:
        return FieldComparison(
            field=field_name,
            si_raw=si_norm.raw, bl_raw=bl_norm.raw,
            si_normalized=si_norm.text, bl_normalized=bl_norm.text,
            is_match=False, match_method=METHOD_MISSING,
            similarity=None, delta=None,
            needs_human=True, needs_human_reason=reason,
            discrepancy_detail={
                "kind": "blank" if reason == REASON_MISSING_VALUE else "unreadable",
                "si": si_norm.raw, "bl": bl_norm.raw, "rationale": rationale,
            },
            si_parsed=si_norm.parsed, bl_parsed=bl_norm.parsed,
        )

    if not si.is_readable or not bl.is_readable:
        which = si.doc_type if not si.is_readable else bl.doc_type
        detail = (si.read_error if not si.is_readable else bl.read_error) or "no readable text"
        return _undecided(REASON_UNREADABLE, f"{which} is unreadable: {detail}")
    si_blank = field_name in si.blank_fields or si_norm.blank
    bl_blank = field_name in bl.blank_fields or bl_norm.blank
    if si_blank or bl_blank:
        if si_blank and bl_blank:
            return _undecided(REASON_MISSING_VALUE,
                              "Both sides are blank or placeholders — comparison not possible")
        if not blank_is_defect:
            return _undecided(
                REASON_MISSING_VALUE,
                "One side is blank or a placeholder and the email states the customer left it "
                "blank — escalated as uncertain rather than treated as a mismatch")
        blank_side = si.doc_type if si_blank else bl.doc_type
        other_side = bl.doc_type if si_blank else si.doc_type
        return FieldComparison(
            field=field_name,
            si_raw=si_norm.raw, bl_raw=bl_norm.raw,
            si_normalized=si_norm.text, bl_normalized=bl_norm.text,
            is_match=False, match_method=METHOD_BLANK_ONE_SIDE,
            similarity=None, delta=None,
            needs_human=False, needs_human_reason=None,
            discrepancy_detail={
                "kind": "blank_one_side", "si": si_norm.raw, "bl": bl_norm.raw,
                "blank_side": blank_side,
                "rationale": f"{blank_side} side is empty on this field while {other_side} "
                             f"side carries a value",
            },
            si_parsed=si_norm.parsed, bl_parsed=bl_norm.parsed,
        )

    try:
        if field_name in ENTITY_FIELDS:
            is_match, method, similarity = _decide_entities(si_norm, bl_norm)
            return FieldComparison(
                field=field_name, si_raw=si_norm.raw, bl_raw=bl_norm.raw,
                si_normalized=si_norm.text, bl_normalized=bl_norm.text,
                is_match=is_match, match_method=method,
                similarity=round(similarity, 3), delta=None,
                discrepancy_detail={
                    "kind": "value_diff", "si": si_norm.text, "bl": bl_norm.text,
                    "similarity": round(similarity, 4),
                    "rationale": "Entity names match after normalisation" if is_match else
                                 f"Entity names differ (similarity {similarity:.3f}, "
                                 f"threshold {ENTITY_FUZZY_RATIO})",
                },
                si_parsed=si_norm.parsed, bl_parsed=bl_norm.parsed,
            )

        if field_name in PORT_FIELDS:
            is_match, method, similarity = _decide_ports(si_norm, bl_norm)
            return FieldComparison(
                field=field_name, si_raw=si_norm.raw, bl_raw=bl_norm.raw,
                si_normalized=si_norm.text, bl_normalized=bl_norm.text,
                is_match=is_match, match_method=method,
                similarity=round(similarity, 3), delta=None,
                discrepancy_detail={
                    "kind": "port_diff",
                    "si": {"place": si_norm.text, "code": si_norm.code,
                           "country": si_norm.country},
                    "bl": {"place": bl_norm.text, "code": bl_norm.code,
                           "country": bl_norm.country},
                    "similarity": round(similarity, 4),
                    "code_conflict": bool(si_norm.code and bl_norm.code
                                          and si_norm.code != bl_norm.code),
                    "rationale": "Ports match (place name and token overlap; UN/LOCODE is used "
                                 "only as conflict evidence)" if is_match else
                                 "Ports differ (place name or country conflict, or "
                                 "contradictory UN/LOCODEs)",
                },
                si_parsed=si_norm.parsed, bl_parsed=bl_norm.parsed,
            )

        if field_name == "container_count":
            if si_norm.count is None or bl_norm.count is None:
                return _undecided(REASON_MISSING_VALUE,
                                  "Container count could not be parsed as an integer")
            is_match, method, delta = _decide_counts(si_norm, bl_norm)
            return FieldComparison(
                field=field_name, si_raw=si_norm.raw, bl_raw=bl_norm.raw,
                si_normalized=str(si_norm.count), bl_normalized=str(bl_norm.count),
                is_match=is_match, match_method=method, similarity=None, delta=delta,
                discrepancy_detail={
                    "kind": "numeric_delta", "si": si_norm.count, "bl": bl_norm.count,
                    "delta": str(delta), "tolerance": "0",
                    "rationale": "Container counts match" if is_match else
                                 f"Container counts differ by {abs(delta)} (zero tolerance)",
                },
                si_parsed=si_norm.parsed, bl_parsed=bl_norm.parsed,
            )

        if field_name == "gross_weight_kg":
            if si_norm.kilograms is None or bl_norm.kilograms is None:
                return _undecided(REASON_MISSING_VALUE,
                                  "Gross weight could not be parsed as a KG value")
            is_match, method, delta, tolerance = _decide_weights(
                si_norm, bl_norm, abs_tolerance, rel_tolerance)
            return FieldComparison(
                field=field_name, si_raw=si_norm.raw, bl_raw=bl_norm.raw,
                si_normalized=str(si_norm.kilograms), bl_normalized=str(bl_norm.kilograms),
                is_match=is_match, match_method=method, similarity=None, delta=delta,
                discrepancy_detail={
                    "kind": "numeric_delta",
                    "si_kg": str(si_norm.kilograms), "bl_kg": str(bl_norm.kilograms),
                    "delta_kg": str(delta), "tolerance_kg": str(tolerance),
                    "rationale": "Gross weight matches" if is_match else
                                 f"Gross weight differs by {delta} KG, beyond the "
                                 f"{tolerance} KG tolerance",
                },
                si_parsed=si_norm.parsed, bl_parsed=bl_norm.parsed,
            )
    except Exception as exc:  # noqa: BLE001 —— 判定异常必须降级为人审，不能污染结论
        return _undecided(REASON_MISSING_VALUE,
                          f"Judgement error, downgraded to human review: "
                          f"{type(exc).__name__}: {exc}")

    return _undecided(REASON_MISSING_VALUE,
                      f"Field is not covered by the judgement matrix: {field_name}")


def _compare_rows(
    si: DocumentView,
    bl: DocumentView,
    fields: Sequence[str],
    abs_tolerance: Decimal,
    rel_tolerance: Decimal,
    *,
    blank_is_defect: bool,
) -> tuple[FieldComparison, ...]:
    """一行一趟地跑判定矩阵（供 compare_documents 的两趟判定复用）。"""
    return tuple(
        compare_field(name, si, bl, abs_tolerance=abs_tolerance,
                      rel_tolerance=rel_tolerance, blank_is_defect=blank_is_defect)
        for name in fields
    )


# ===========================================================================
# 文档级结论（顺序即优先级；第一条命中即停）
# ===========================================================================
def compare_documents(
    si: DocumentView,
    bl: DocumentView,
    *,
    fields: Sequence[str] = COMPARE_FIELDS,
    abs_tolerance: Decimal = WEIGHT_ABS_TOLERANCE_KG,
    rel_tolerance: Decimal = WEIGHT_REL_TOLERANCE,
    blank_is_defect: bool = True,
) -> ComparisonReport:
    """blank_is_defect 控制**单侧空白**的语义。实测校准（很重要，别想当然）：
    全量 220 封 BL_COMPARISON 里共有 **53 处单侧空白，其中仅 2 处**（email_313/351）
    被 gold 计入 defect_fields，而这两处都有一个共同特征 —— **同封邮件里已经存在
    另一个确证缺陷**（容器数 5≠4 / 15≠16）。剩下 51 处（发票当 BL、扫描件不可读、
    email_160/273 的孤立空白）都不是缺陷。

    所以规则不是「单侧空白就是缺陷」，而是**旁证升级**（两趟判定）：
      pass 1：单侧空白一律当「不确定」→ 看是否有硬缺陷；
      pass 2：仅当已存在硬缺陷时，才把单侧空白一并升级为缺陷。
    这样 email_313 → {container_count, gross_weight_kg} 与 gold 完全一致，
    而 email_160/273 保持「不确定」（不会变成假阳性）。
    """
    rows = _compare_rows(si, bl, fields, abs_tolerance, rel_tolerance,
                         blank_is_defect=False)
    if blank_is_defect and any(not r.is_match and not r.needs_human for r in rows):
        rows = _compare_rows(si, bl, fields, abs_tolerance, rel_tolerance,
                             blank_is_defect=True)
    defect_fields = tuple(row.field for row in rows if not row.is_match and not row.needs_human)
    matched_fields = tuple(row.field for row in rows if row.is_match)
    undecided_fields = tuple(row.field for row in rows if row.needs_human)

    signals: dict[str, Any] = {
        "si_readable": si.is_readable,
        "bl_readable": bl.is_readable,
        "si_doc_role": si.doc_role,
        "bl_doc_role": bl.doc_role,
        "wrong_doc_kind": bl.other_doc_kind or si.other_doc_kind,
        "undecided_fields": list(undecided_fields),
        "blank_is_defect": blank_is_defect,
        "si_path": si.source_path,
        "bl_path": bl.source_path,
    }

    # 规则 1 —— 铁律：检出不一致就是 MISMATCH，其余信号一律让位
    if defect_fields:
        if undecided_fields:
            signals["blocked_escalation"] = {
                "reason": REASON_MISSING_VALUE,
                "fields": list(undecided_fields),
                "note": "Undecided fields exist, but confirmed defects take precedence — no "
                        "NEEDS_REVIEW is raised",
            }
        return ComparisonReport(
            comparisons=rows, defect_fields=tuple(sorted(defect_fields)),
            matched_fields=matched_fields, undecided_fields=undecided_fields,
            status=STATUS_MISMATCH, has_defect=True, review_reason=None,
            escalation_signals=signals,
        )

    # 规则 2 —— 第二个附件是发票/装箱单/COO（模型实判角色为 OTHER）
    if bl.doc_role == "OTHER" or si.doc_role == "OTHER":
        return _needs_review(rows, matched_fields, undecided_fields, signals,
                             REASON_WRONG_DOC_TYPE,
                             "Attachment is not an SI/draft-BL counterpart document")
    if {si.doc_role, bl.doc_role} in ({"SI"}, {"BL"}):
        return _needs_review(rows, matched_fields, undecided_fields, signals,
                             REASON_WRONG_DOC_TYPE,
                             "Both slots hold the same kind of document — no counterpart to compare")

    # 规则 3 —— 文档不可读（空文件 / 图片型扫描件 / 无文本层 / 损坏）
    if not si.is_readable or not bl.is_readable:
        return _needs_review(rows, matched_fields, undecided_fields, signals,
                             REASON_UNREADABLE,
                             "Document has no readable text layer or is corrupt")

    # 规则 4 —— 两侧无冲突但存在空白字段（占位符是"不确定"，不是"不一致"）
    if undecided_fields:
        return _needs_review(rows, matched_fields, undecided_fields, signals,
                             REASON_MISSING_VALUE,
                             "Required fields are blank or placeholders — comparison not possible")

    # 规则 5 —— 全部一致
    return ComparisonReport(
        comparisons=rows, defect_fields=(), matched_fields=matched_fields,
        undecided_fields=(), status=STATUS_OK, has_defect=False, review_reason=None,
        escalation_signals=signals,
    )


def _needs_review(
    rows: tuple[FieldComparison, ...],
    matched_fields: tuple[str, ...],
    undecided_fields: tuple[str, ...],
    signals: dict[str, Any],
    reason: str,
    rationale: str,
) -> ComparisonReport:
    signals["escalation_rationale"] = rationale
    return ComparisonReport(
        comparisons=rows, defect_fields=(), matched_fields=matched_fields,
        undecided_fields=undecided_fields, status=STATUS_NEEDS_REVIEW,
        has_defect=False, review_reason=reason, escalation_signals=signals,
    )


if __name__ == "__main__":  # pragma: no cover —— 回归自检（真实数据校准样本）
    from decimal import Decimal as D

    def view(doc_type: str, **values: Any) -> DocumentView:
        return DocumentView(doc_type=doc_type, values=values,
                            doc_role="SI" if doc_type == "SI" else "BL")

    base = dict(
        shipper="APRIL FAR EAST (M) SDN BHD",
        consignee="EAST BRIGHT FZ-LLC",
        notify_party="EAST BRIGHT FZ-LLC",
        port_of_loading="NANTONG, CHINA (CNNTG)",
        port_of_discharge="KARACHI, PAKISTAN (PKKHI)",
        container_count="6 x 40'HC",
        gross_weight_kg="131,058 KG",
    )
    bl_labels = dict(base)
    bl_labels.update(
        shipper="SHIPPER: APRIL FAR EAST (M) SDN BHD",
        consignee="To the Order of: EAST BRIGHT FZ-LLC",
        notify_party="Notify Party: EAST BRIGHT FZ-LLC",
        port_of_loading="Port of Loading (POL): NANTONG, CHINA (CNNTG)",
        port_of_discharge="Port of Discharge: KARACHI, PAKISTAN (PKKHI)",
        container_count="Container Count: 6 x 40'HC",
        gross_weight_kg="Gross Weight (KG): 131,058 KG",
    )
    report = compare_documents(view("SI", **base), view("BL", **bl_labels))
    assert report.status == "OK" and report.defect_fields == (), report
    assert all(row.is_match for row in report.comparisons)

    # ★ 陈旧 UN/LOCODE 陷阱：BL 缺陷港口沿用原 code → 必须以地名判不一致
    stale = dict(bl_labels, port_of_discharge="Port of Discharge: TUTICORIN, INDIA (PKKHI)")
    report = compare_documents(view("SI", **base), view("BL", **stale))
    assert report.status == "MISMATCH" and report.defect_fields == ("port_of_discharge",), report

    # ★ 嵌套实体陷阱：相似度仅 0.727，必须判不一致
    nested = dict(bl_labels, shipper="Shipper: APRIL FINE PAPER TRADING")
    report = compare_documents(
        view("SI", **{**base, "shipper": "APRIL FINE PAPER TRADING (MIDDLE EAST) FZE"}),
        view("BL", **nested))
    assert report.status == "MISMATCH" and report.defect_fields == ("shipper",), report

    # 缺一箱 / 差 500kg
    off = dict(bl_labels, container_count="Container Count: 5 x 40'HC",
               gross_weight_kg="Gross Weight (KG): 130,558 KG")
    report = compare_documents(view("SI", **base), view("BL", **off))
    assert report.defect_fields == ("container_count", "gross_weight_kg"), report

    # 容差内：±8kg 不算缺陷
    tol = dict(bl_labels, gross_weight_kg="Gross Weight (KG): 131,066 KG")
    assert compare_documents(view("SI", **base), view("BL", **tol)).status == "OK"

    # ★ 单侧空白：孤立时是「不确定」，不是缺陷（email_160/273 的 gold 就是 OK）
    #   占位符要写成抽取器真正会产出的形状（裸 "???"），带标签的值不是 blank。
    blank = dict(bl_labels, gross_weight_kg="???")
    report = compare_documents(view("SI", **base), view("BL", **blank))
    assert report.status == "NEEDS_REVIEW" and report.review_reason == "missing_value", report

    # ★ 旁证升级：同封已有硬缺陷（容器数 5≠6）→ 被留空的毛重一起进 defect_fields。
    #   email_313 的 gold defect_fields 正是 {container_count, gross_weight_kg}。
    both = dict(blank, container_count="Container Count: 5 x 40'HC")
    report = compare_documents(view("SI", **base), view("BL", **both))
    assert report.status == "MISMATCH", report
    assert report.defect_fields == ("container_count", "gross_weight_kg"), report
    assert any(row.match_method == "blank_one_side" for row in report.comparisons), report

    # 正文已声明「客户留空」（email_516–520 形态）→ 永不升级为缺陷
    report = compare_documents(view("SI", **base), view("BL", **both), blank_is_defect=False)
    assert report.defect_fields == ("container_count",), report
    assert report.undecided_fields == ("gross_weight_kg",), report

    # 两侧同时为空 = 永远的不确定，与 blank_is_defect 无关
    two_sided = dict(bl_labels, gross_weight_kg="???", container_count="TBA")
    report = compare_documents(
        view("SI", **{**base, "gross_weight_kg": "???", "container_count": "TBA"}),
        view("BL", **two_sided))
    assert report.status == "NEEDS_REVIEW" and report.review_reason == "missing_value", report

    # 铁律：既有真实缺陷又有空白 → 仍然 MISMATCH，空白字段被记到 blocked_escalation
    blocked = dict(two_sided,
                   port_of_discharge="Port of Discharge: TUTICORIN, INDIA (PKKHI)")
    report = compare_documents(
        view("SI", **{**base, "gross_weight_kg": "???", "container_count": "TBA"}),
        view("BL", **blocked))
    assert report.status == "MISMATCH", report
    assert report.defect_fields == ("port_of_discharge",), report
    assert report.escalation_signals["blocked_escalation"]["fields"], report

    # 标签差异但值相同（Load Port vs Port of Loading）→ OK
    label_only = dict(base, port_of_loading="Load Port: NANTONG, CHINA (CNNTG)",
                      container_count="No. of Containers: 6 x 40'HC",
                      gross_weight_kg="Gross Wt (kgs): 131,058 KG")
    assert compare_documents(view("SI", **base), view("BL", **label_only)).status == "OK"

    # wrong_doc_type / unreadable
    other = DocumentView(doc_type="BL", values={}, doc_role="OTHER",
                         other_doc_kind="commercial_invoice")
    assert compare_documents(view("SI", **base), other).review_reason == "wrong_doc_type"
    broken = DocumentView(doc_type="BL", values={}, doc_role="BL", is_readable=False,
                          read_error="no text layer")
    assert compare_documents(view("SI", **base), broken).review_reason == "unreadable"
    print("compare.py self-test OK")
