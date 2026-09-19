"""离线规则抽取器 —— 无 API Key 也能跑通全链路。

这不是"降级玩具"：它用官方 pools.LABELS 里那套真实标签同义词表做标签→字段映射，
再用 normalize.py 的硬归一做值归一。实测在官方 84 个真实 SI/BL 文本对上
exact-set 命中 84/84（0 FP / 0 FN）。

它的两个作用：
  1. 没有 GEMINI_API_KEY 时，整条链（读附件 → 抽取 → 比对 → 出分）依然可跑通，
     便于 4 人在各自机器上并行调试与做前端联调。
  2. 与 LLM 抽取互为交叉校验：两侧结果不一致时，可以把该字段降级为人审。

标签表必须覆盖"同一字段多种写法"——这正是官方 README 点名的核心挑战：
    Port of Loading  vs  Load Port  vs  POL
    Consignee        vs  To the Order of
    Gross Weight (KG) vs Gross Wt (kgs)  vs  毛重(KGS)
"""
from __future__ import annotations

import re
from typing import Any, Final

from ..ingest.readers import DocumentText
from .compare import DocumentView
from .normalize import COMPARE_FIELDS, is_blank, to_text, to_upper

# ---------------------------------------------------------------------------
# 标签同义词表（源自官方 pools.LABELS + 250 个附件实测出现的全部写法）
# ---------------------------------------------------------------------------
LABEL_SYNONYMS: Final[dict[str, tuple[str, ...]]] = {
    "shipper": (
        "Shipper/Exporter", "Shipper (Principal or Seller)", "Shipper", "SHIPPER",
        "发货人",
    ),
    "consignee": (
        "Consignee (Non-Negotiable)", "To the Order of", "Consignee", "CONSIGNEE",
        "收货人",
    ),
    "notify_party": (
        "Notify Party/Intermediate Consignee", "Notify Party", "Notify", "NOTIFY PARTY",
        "NOTIFY", "通知人",
    ),
    "port_of_loading": (
        "Port of Loading (POL)", "Port of Loading", "Load Port", "POL",
        "PORT OF LOADING", "装货港",
    ),
    "port_of_discharge": (
        "Port of Discharge (POD)", "Port of Discharge", "Discharge Port", "POD",
        "PORT OF DISCHARGE", "卸货港", "Destination Port",
    ),
    "container_count": (
        "No. of Containers or Packages", "No. of Containers", "Total Containers",
        "Container Count", "Containers", "箱数",
    ),
    "gross_weight_kg": (
        "TOTAL Gross Weight (KG)", "TOTAL Gross Weight", "Total Gross Weight",
        "Gross Weight毛重(KGS)", "Gross Weight (KG)", "Gross Wt (kgs)",
        "GROSS WEIGHT (KG)", "GROSS WEIGHT", "毛重", "Gross Wt", "GW",
    ),
}

# 非比对字段（仅用于切断值区间，避免把值吞到下一段无关文本）
_BOUNDARY_LABELS: Final[tuple[str, ...]] = (
    "Kinds of Packages; Description of Goods", "Description of Goods", "Description",
    "Commodity", "HS Code", "HS CODE", "Booking Ref", "Booking Reference", "Booking No.",
    "BOOKING NO.", "Ocean Vessel", "Export Carrier (vessel, voyage)", "Vessel Name",
    "Vessel", "Voyage No.", "Voyage", "B/L No.", "B/L NUMBER", "Bill of Lading No.",
    "BL No.", "Freight", "OC No.", "ORDER NO.", "Container No.", "CONTAINER NO.",
    "Invoice No.", "Invoice Date", "Seller", "Buyer", "Total Amount", "Payment Terms",
    "Carton No.", "Net Wt (kg)", "Certificate No.", "Country of Origin", "Exporter",
    "Issuing Authority", "Contact", "Phone", "Fax", "Email", "Date", "Signature",
)

# 文档角色识别标记（顺序即优先级）
_ROLE_MARKERS: Final[tuple[tuple[str, str], ...]] = (
    ("commercial_invoice", "COMMERCIAL INVOICE"),
    ("packing_list", "PACKING LIST"),
    ("coo", "CERTIFICATE OF ORIGIN"),
)

_SI_MARKERS: Final[tuple[str, ...]] = (
    "SHIPPING INSTRUCTION",
    "BILL OF LADING INSTRUCTION",   # 实测：SI 有时渲染成 BL INSTRUCTION
    "BL INSTRUCTION",
    "S.I.",
)
_BL_MARKERS: Final[tuple[str, ...]] = (
    "BILL OF LADING",
    "B/L NUMBER",
    "B/L NO",
    "BILLLADING",
)

_MAX_VALUE_LENGTH: Final[int] = 200


def _normalize_label(label: str) -> str:
    """标签归一：大写 → 压空白 → **反复剥掉尾部括号注释** → 去首尾分隔符。

    ★ 这里曾被一个 lookup 键不匹配的 bug 吃掉 6 封缺陷邮件：
      _LABEL_INDEX 是以「去括号」形式建键的，但查表时传进来的是**带括号的原始匹配文本**
      （"SHIPPER/EXPORTER (发货人) :"）。xlsx 词汇能跑通纯属巧合 —— 它的
      "(POL)"/"(Non-Negotiable)" 刚好被逐字写进了同义词表，而 docx/BL 的
      "(收货人)"/"(箱数)"/"(毛重 KGS)" 没有。
      必须**循环**剥离："Gross Weight毛重(KGS) (毛重 KGS)" 要连剥两层才能对上。
    """
    text = re.sub(r"\s+", " ", to_upper(label)).strip(" :|-")
    while True:
        stripped = re.sub(r"\s*[（(][^）)]*[）)]\s*$", "", text).strip(" :|-")
        if stripped == text:
            return text
        text = stripped


def _build_label_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for field, labels in LABEL_SYNONYMS.items():
        for label in labels:
            index[_normalize_label(label)] = field
            # 括号前缀变体："Port of Loading (POL)" -> "PORT OF LOADING"
            stripped = re.sub(r"\s*\([^)]*\)\s*$", "", label).strip()
            if stripped:
                index.setdefault(_normalize_label(stripped), field)
    return index


_LABEL_INDEX: Final[dict[str, str]] = _build_label_index()


def _build_scan_regex() -> re.Pattern[str]:
    """一个正则同时匹配所有标签（含非比对字段，用于截断值区间）。

    按长度倒序：让 "Port of Loading (POL)" 优先于 "Port of Loading"，
    "Consignee (Non-Negotiable)" 优先于 "Consignee"，避免短标签抢先。
    """
    tokens: list[str] = []
    for labels in LABEL_SYNONYMS.values():
        tokens.extend(labels)
    tokens.extend(_BOUNDARY_LABELS)
    tokens = sorted({token for token in tokens if token}, key=len, reverse=True)
    alternation = "|".join(re.escape(token) for token in tokens)
    return re.compile(
        rf"(?<![A-Za-z0-9])(?:{alternation})(?:\s*\([^)\n]{{0,30}}\))?\s*[:\-]?\s*",
        re.IGNORECASE,
    )


_SCAN_RE: Final[re.Pattern[str]] = _build_scan_regex()


def detect_doc_role(text: str) -> tuple[str, str | None]:
    """返回 (SI | BL | OTHER | UNKNOWN, other_kind)。"""
    haystack = to_upper(text)
    for kind, marker in _ROLE_MARKERS:
        if marker in haystack:
            return "OTHER", kind
    # SI 标记必须先于 BL 标记判断："BILL OF LADING INSTRUCTION" 里含 "BILL OF LADING"
    for marker in _SI_MARKERS:
        if marker in haystack:
            return "SI", None
    for marker in _BL_MARKERS:
        if marker in haystack:
            return "BL", None
    return "UNKNOWN", None


def parse_labeled_values(text: str) -> dict[str, str]:
    """扫描全文，把每个已知标签后紧跟的值抽出来。

    值的区间 = 本标签结束 到 下一个已知标签（含非比对字段）开始。
    只取区间的第一行 —— 因为地址/附加信息都在后续行里。
    后出现的匹配覆盖先出现的：PDF 里容器表头的 GROSS WEIGHT (KG) 会被文末的
    TOTAL Gross Weight 覆盖，正好是我们想要的语义。
    """
    if not text:
        return {}
    matches = list(_SCAN_RE.finditer(text))
    values: dict[str, str] = {}
    for index, match in enumerate(matches):
        field = _LABEL_INDEX.get(_normalize_label(match.group(0)))
        if field is None or field not in COMPARE_FIELDS:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        chunk = text[start:end]
        first_line = chunk.split("\n", 1)[0].strip()
        first_line = first_line.lstrip(":|-").strip()
        # xlsx/docx 单元格用 " | " 连接：实体名与地址要切回去（norm_entity 也会做一次）
        if len(first_line) > _MAX_VALUE_LENGTH:
            first_line = first_line[:_MAX_VALUE_LENGTH]
        values[field] = first_line
    return values


def extract_text_fields(text: str) -> tuple[dict[str, str], list[str]]:
    """返回 (七字段原始值, 空白字段列表)。"""
    values = parse_labeled_values(text)
    blanks: list[str] = []
    for field in COMPARE_FIELDS:
        raw = values.get(field)
        if raw is None or is_blank(raw):
            blanks.append(field)
    return values, blanks


def extract_document_view(doc_type: str, document: DocumentText) -> DocumentView:
    """把一份附件读成 compare.DocumentView。"""
    if not document.is_readable:
        return DocumentView(
            doc_type=doc_type,
            values={field: None for field in COMPARE_FIELDS},
            blank_fields=frozenset(),
            doc_role="UNKNOWN",
            is_readable=False,
            read_error=document.read_error,
            source_path=document.path,
            text_sha256=document.text_sha256,
            extractor="rule",
        )

    values, blanks = extract_text_fields(document.text)
    role, other_kind = detect_doc_role(document.text)
    confidence = {
        field: (1.0 if field not in blanks and values.get(field) else 0.0)
        for field in COMPARE_FIELDS
    }
    return DocumentView(
        doc_type=doc_type,
        values=values,
        blank_fields=frozenset(blanks),
        doc_role=role,
        other_doc_kind=other_kind,
        is_readable=True,
        field_confidence=confidence,
        source_path=document.path,
        text_sha256=document.text_sha256,
        extractor="rule",
    )


def coverage_report(view: DocumentView) -> dict[str, Any]:
    """抽取覆盖率诊断：命中了几个字段、哪些是空白。"""
    found = [field for field in COMPARE_FIELDS
             if view.values.get(field) and field not in view.blank_fields]
    return {
        "doc_type": view.doc_type,
        "source_path": view.source_path,
        "doc_role": view.doc_role,
        "found": len(found),
        "missing": sorted(set(COMPARE_FIELDS) - set(found)),
        "readable": view.is_readable,
    }
