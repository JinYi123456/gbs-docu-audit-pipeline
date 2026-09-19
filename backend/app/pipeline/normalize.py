"""硬归一层 —— 纯标准库，零外部依赖（可在无网络、无 SDK 环境下单测）。

契约：LLM 只负责「定位与语义对齐」，本模块负责「归一与可判定化」。
比对与判定只看本模块的输出，因此这里是精度的唯一真相。

★★ 本文件的归一规则已用官方 84 个真实 SI/BL 文本对离线校准，结论：
    · 84/84 干净对在 norm_entity 归一后**完全相等**（无需模糊匹配）
    · 注入缺陷的最小相似度 = 0.727（嵌套陷阱 APRIL FINE PAPER TRADING
      ⊂ APRIL FINE PAPER TRADING (MIDDLE EAST) FZE 真实出现过）
    · 港口：BL 注入缺陷时**沿用原 UN/LOCODE**，故 code 只能作冲突证据
    · 两个必须守住的字符串坑：(WESTPORT) 是港名组成部分；CHINA/KENYA/INDIA
      都符合 [A-Z]{2}[A-Z0-9]{3} 形状，不能当港口代码吞掉
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Mapping

# ---------------------------------------------------------------------------
# 7 个 Canonical 字段（与官方 pools.COMPARE_FIELDS / sdoc_compare_field enum 逐字一致）
# ---------------------------------------------------------------------------
COMPARE_FIELDS: Final[tuple[str, ...]] = (
    "shipper",
    "consignee",
    "notify_party",
    "port_of_loading",
    "port_of_discharge",
    "container_count",
    "gross_weight_kg",
)
ENTITY_FIELDS: Final[frozenset[str]] = frozenset({"shipper", "consignee", "notify_party"})
PORT_FIELDS: Final[frozenset[str]] = frozenset({"port_of_loading", "port_of_discharge"})
NUMERIC_FIELDS: Final[frozenset[str]] = frozenset({"container_count", "gross_weight_kg"})

# ---------------------------------------------------------------------------
# 占位符/空白标记（官方 edgecases.BLANK_TOKENS + 真实单据常见写法）
# 空白 = 不确定，绝不等于 0，也绝不等于不一致
# ---------------------------------------------------------------------------
BLANK_TOKENS: Final[frozenset[str]] = frozenset({
    "", "?", "??", "???", "______", "_______", "____", "_____",
    "TBA", "TBC", "TBD", "N/A", "NA", "NIL", "NONE", "-", "--", "---",
    "0?", "____MT", "TO BE ADVISED", "TO BE CONFIRMED", "AS PER ATTACHED", "SEE ATTACHED",
})
_BLANK_SHAPE_RE: Final[re.Pattern[str]] = re.compile(r"^[_\-.?\s]*$")

_ENTITY_LABEL_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:SHIPPER(?:\s*/\s*EXPORTER)?|CONSIGNEE|NOTIFY(?:\s+PARTY)?"
    r"(?:\s*/\s*INTERMEDIATE\s+CONSIGNEE)?"
    r"|TO\s+THE\s+ORDER\s+OF|SHIPPER\s*\(\s*PRINCIPAL\s+OR\s+SELLER\s*\))"
    r"\s*(?:\([^)]*\))?\s*[:\-]?\s*",
    re.IGNORECASE,
)
# 同一字段的多种标签写法（docx/BL 词汇）——用于判断"这一段只是标签、根本不是值"
_ENTITY_LABEL_ONLY_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:SHIPPER(?:\s*/\s*EXPORTER)?|CONSIGNEE|TO\s+THE\s+ORDER\s+OF|"
    r"NOTIFY(?:\s+PARTY)?(?:\s*/\s*INTERMEDIATE\s+CONSIGNEE)?)"
    r"\s*(?:\([^)]*\))?\s*[:\-|]?\s*$",
    re.IGNORECASE,
)
# 仅剥离"元信息"括号；绝不能剥离 (SINGAPORE) PTE LTD / (M) SDN BHD / FZ-LLC 这类实体名成分
_ENTITY_METADATA_PAREN_RE: Final[re.Pattern[str]] = re.compile(
    r"\((?:NON[-\s]?NEGOTIABLE|PRINCIPAL\s+OR\s+SELLER|ORIGINAL|COPY|TO\s+ORDER"
    r"|UPPER\s+CASE|PLEASE\s+PRINT)\)",
    re.IGNORECASE,
)

# 值里可能还带着标签（LLM 通道常见："Port of Loading (POL): NANTONG, CHINA (CNNTG)"）。
# ★ 只剥离**带分隔符**的标签："PORT KLANG (WESTPORT), MALAYSIA" 没有冒号/横线，绝不能被吃。
_PORT_LABEL_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:PORTS?\s+OF\s+(?:LOADING|DISCHARGE|DESTINATION)|LOAD(?:ING)?\s+PORT"
    r"|DISCHARGE\s+PORT|DESTINATION\s+PORT|POL|POD)"
    r"\s*(?:\([^)]*\))?\s*[:\-]\s*",
    re.IGNORECASE,
)

# 港口代码只接受"括号内、5 字符 UNLOCODE 形状、且不是国家名"的内容
_PORT_CODE_PAREN_RE: Final[re.Pattern[str]] = re.compile(r"\(([A-Z]{2}[A-Z0-9]{3})\)")
_COUNTRY_GUARD: Final[frozenset[str]] = frozenset({
    "CHINA", "KENYA", "INDIA", "JAPAN", "KOREA", "CHILE", "PERU", "GUINEA",
    "NIGERIA", "TURKEY", "ISRAEL", "POLAND", "UAE", "MALAYSIA", "BRAZIL",
    "EGYPT", "SPAIN", "ITALY", "FRANCE", "GERMANY", "VIETNAM", "THAILAND",
    "SINGAPORE", "PAKISTAN", "MYANMAR", "AUSTRALIA", "JORDAN", "SLOVENIA",
    "LITHUANIA", "GHANA", "OMAN", "QATAR", "YEMEN", "SUDAN", "GREECE",
    "PORTUGAL", "BELGIUM", "MEXICO", "CANADA", "NORWAY", "SWEDEN", "DENMARK",
})
_COUNTRY_ALIAS: Final[Mapping[str, str]] = {
    "USA": "US", "U.S.A": "US", "U.S.A.": "US", "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US", "UAE": "AE", "UNITED ARAB EMIRATES": "AE",
    "SOUTH KOREA": "KR", "KOREA": "KR", "REPUBLIC OF KOREA": "KR",
    "UK": "GB", "UNITED KINGDOM": "GB", "GREAT BRITAIN": "GB",
    "RUSSIA": "RU", "NETHERLANDS": "NL", "HOLLAND": "NL",
    "SAUDI ARABIA": "SA", "SOUTH AFRICA": "ZA", "HONG KONG": "HK",
}

_WEIGHT_UNIT_MULTIPLIERS: Final[tuple[tuple[re.Pattern[str], Decimal], ...]] = (
    (re.compile(r"\b(?:MT|MTS|TON|TONS|TONNE|TONNES|公吨)\b"), Decimal("1000")),
    (re.compile(r"\b(?:KG|KGS|KILOGRAM|KILOGRAMS|公斤)\b"), Decimal("1")),
    (re.compile(r"\b(?:LB|LBS|POUNDS?)\b"), Decimal("0.45359237")),
    (re.compile(r"\b(?:G|GR|GRAM|GRAMS)\b"), Decimal("0.001")),
)
_WEIGHT_NUMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
)
_WEIGHT_MAX_KG: Final[Decimal] = Decimal("50000000")   # 5 万吨，超过即视为解析失败
_WEIGHT_QUANTUM: Final[Decimal] = Decimal("0.001")     # numeric(14,3)

_COUNT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # "6 x 40'HC" / "6 X 40HC" / "6*40 DV"
    re.compile(r"(?<![\d,])(\d{1,3}(?:,\d{3})+|\d+)\s*[X*]\s*\d+\s*(?:'|\u2019|FT|FCL|HC|GP|DV|OT|FR)"),
    # "TOTAL 6 CONTAINERS" / "6 CTRS"
    re.compile(r"(?<![\d,])(\d{1,3}(?:,\d{3})+|\d+)\s*(?:CONTAINERS?|CTRS?|BOXES?|PACKAGES?)\b"),
    # "SIX (6) FORTY FOOT"
    re.compile(r"\((\d+)\)"),
    # 兜底：行内第一个独立整数，但拒绝 40'HC / 40FT 这类尺寸值
    re.compile(r"(?<![\d,])(\d{1,3}(?:,\d{3})+|\d+)(?![\d'\u2019]*\s*(?:FT|HC|GP|DV|FCL))"),
)
_COUNT_WORDS: Final[Mapping[str, int]] = {
    "ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5, "SIX": 6,
    "SEVEN": 7, "EIGHT": 8, "NINE": 9, "TEN": 10, "ELEVEN": 11, "TWELVE": 12,
    "FIFTEEN": 15, "TWENTY": 20,
}


# ===========================================================================
# 基础工具
# ===========================================================================
def to_text(value: Any) -> str:
    """任意输入 → 单个干净字符串：NFKC、NBSP→空格、折叠空白、去首尾。"""
    if value is None:
        return ""
    if isinstance(value, str):
        raw = value
    elif isinstance(value, (int, float, Decimal)):
        raw = str(value)
    elif isinstance(value, bytes):
        raw = value.decode("utf-8", errors="replace")
    else:
        raw = str(value)
    raw = raw.replace("\u00a0", " ").replace("\u2007", " ").replace("\ufeff", "")
    raw = unicodedata.normalize("NFKC", raw)
    return re.sub(r"\s+", " ", raw).strip()


def to_upper(value: Any) -> str:
    return to_text(value).upper()


def collapse_lines(value: Any) -> list[str]:
    """按行/竖线/分号切分，返回非空片段（xlsx 单元格用 ' | ' 连接，需切回）。

    ★ 必须在 to_text **之前**切分：to_text 会把 \n 折叠成空格，
      那样「实体名\n地址」就再也分不开了 —— norm_entity 的第 1 条规则会静默失效。
    """
    if value is None:
        return []
    raw = value if isinstance(value, str) else to_text(value)
    if not raw:
        return []
    chunks = re.split(r"\r\n|\r|\n|\s+\|\s+|\s*;\s*", raw)
    return [cleaned for cleaned in (to_text(chunk) for chunk in chunks) if cleaned]


def is_blank(value: Any) -> bool:
    """占位符 / 空白 / 纯符号 判定。None 与空串都算 blank。"""
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float, Decimal)):
        return False
    text = to_upper(value)
    if not text:
        return True
    if text in BLANK_TOKENS:
        return True
    return bool(_BLANK_SHAPE_RE.match(text))


# ===========================================================================
# norm_entity —— 实体名归一（Shipper / Consignee / Notify Party）
# ===========================================================================
def norm_entity(value: Any) -> str | None:
    """抽取实体法定名称并归一到可比字符串。

    规则：
      1. 多行/分隔符 → 只取实体名那一段（地址在后续行，直接丢弃）
      2. 去掉前缀标签（SHIPPER: / To the Order of: / Consignee (Non-Negotiable): ）
      3. 只剥离元信息括号，保留 (SINGAPORE) / (M) / (MIDDLE EAST) 等实体名成分
      4. '&' → 'AND'；去掉 '.' 与 ','（"CO., LTD" 与 "CO. LTD" 必须相等）
      5. 大写 + 折叠空白

    返回 None 表示该字段不可判定（blank）。
    """
    if is_blank(value):
        return None
    segments = collapse_lines(value)
    if not segments:
        return None
    # 正常情况下第一段就是实体名；但若第一段**只是标签**（docx/BL 写成
    # "CONSIGNEE (收货人) | VITAL SOLUTIONS PTE. LTD. | …"，而 LLM 通道也常这么回），
    # 就直接取下一段，否则会把整个实体判成 None（静默丢字段）。
    entity = segments[0]
    if _ENTITY_LABEL_ONLY_RE.match(entity.strip()):
        entity = next((segment for segment in segments[1:4]
                       if not _ENTITY_LABEL_ONLY_RE.match(segment.strip())), entity)
    entity = _ENTITY_LABEL_PREFIX_RE.sub("", entity)
    entity = _ENTITY_METADATA_PAREN_RE.sub(" ", entity)
    entity = entity.replace("&", " AND ")
    entity = entity.replace(".", " ").replace(",", " ")
    entity = re.sub(r"[^A-Za-z0-9()/\-\s]", " ", entity)
    # 只剥离首尾的分隔符与空格。
    # ★ 绝不能用 .strip(" -/()")：那会把 "ORIENT LINKS CO (LLC)" 收尾的右括号吃掉。
    # ★ 也不做"括号内侧去空格"：那会把 "APRIL FAR EAST (M) SDN BHD" 压成
    #   "APRIL FAR EAST(M)SDN BHD"，污染人审界面与 evidence。
    entity = re.sub(r"\s+", " ", entity).strip(" -/").strip()
    return entity.upper() or None


def entity_tokens(normalized: str | None) -> frozenset[str]:
    if not normalized:
        return frozenset()
    return frozenset(token for token in re.split(r"[^A-Z0-9]+", normalized) if token)


# ===========================================================================
# norm_port —— 港口归一（城市 + 国家 + UN/LOCODE）
# ===========================================================================
@dataclass(slots=True, frozen=True)
class PortParts:
    place: str              # "PORT KLANG (WESTPORT), MALAYSIA" —— 保留非 UNLOCODE 括号
    city: str               # "PORT KLANG (WESTPORT)"
    country: str | None     # "MY"（别名归一后）
    country_raw: str | None
    code: str | None        # "MYPKG"
    tokens: frozenset[str]

    @property
    def is_empty(self) -> bool:
        return not self.place and self.code is None


def norm_port(value: Any) -> PortParts | None:
    """港口归一。严格区分两类括号：
      · (CNNTG) 5 字符 UNLOCODE 形状且不在国家名护栏内 → 识别为 code 并从 place 剔除
      · (WESTPORT) / (SINGAPORE) / (MIDDLE EAST) → 港名组成，必须保留
    """
    if is_blank(value):
        return None
    raw = _PORT_LABEL_PREFIX_RE.sub("", to_upper(value))

    codes: list[str] = []
    for match in _PORT_CODE_PAREN_RE.finditer(raw):
        candidate = match.group(1)
        if candidate not in _COUNTRY_GUARD:
            codes.append(candidate)

    place = raw
    for code in codes:
        place = place.replace(f"({code})", " ")
    # ★ 只清空括号，绝不平掉所有括号：
    #   "PORT KLANG (WESTPORT), MALAYSIA" 里的 (WESTPORT) 是**港名组成部分**，
    #   压成 "PORT KLANG WESTPORT , MALAYSIA" 既违背本函数契约，也让人审界面更难读。
    #   UN/LOCODE 那类括号已经在上面按 code 精确剔除。
    place = re.sub(r"\(\s*\)", " ", place)
    place = re.sub(r"\s+", " ", place).strip(" ,;-")
    if not place and not codes:
        return None

    segments = [segment.strip() for segment in place.split(",") if segment.strip()]
    if len(segments) >= 2:
        country_raw = segments[-1]
        city = ", ".join(segments[:-1])
    elif segments:
        country_raw = None
        city = segments[0]
    else:
        country_raw = None
        city = place

    country = _COUNTRY_ALIAS.get(country_raw, country_raw) if country_raw else None
    if country:
        country = country.upper()
    tokens = frozenset(token for token in re.split(r"[^A-Z0-9]+", city.upper()) if token)
    return PortParts(place=place, city=city, country=country, country_raw=country_raw,
                     code=codes[0] if codes else None, tokens=tokens)


# ===========================================================================
# norm_count —— 集装箱数量归一为 int
# ===========================================================================
def norm_count(value: Any) -> int | None:
    """'6 x 40\\'HC' → 6 | 'Total Containers: 6' → 6 | '???' → None"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and float(value).is_integer() else None
    if is_blank(value):
        return None

    text = to_upper(value)
    for pattern in _COUNT_PATTERNS:
        for match in pattern.finditer(text):
            token = match.group(1).replace(",", "")
            if not token.isdigit():
                continue
            number = int(token)
            if 0 <= number <= 9999:
                return number
    for word, number in _COUNT_WORDS.items():
        if re.search(rf"\b{word}\b", text):
            return number
    return None


# ===========================================================================
# norm_weight —— 毛重统一换算为 KG（Decimal）
# ===========================================================================
def norm_weight(value: Any) -> Decimal | None:
    """'131,058 KG' → 131058.000 | '131.058 MT' → 131058.000 | 131058 → 131058.000

    契约：数值型输入视为**已经是 KG**（由 Prompt B 保证）；字符串按其自带单位解析。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return _finish_weight(value)
    if isinstance(value, int):
        return _finish_weight(Decimal(value))
    if isinstance(value, float):
        return _finish_weight(Decimal(str(value)))
    if is_blank(value):
        return None

    text = to_upper(value)
    multiplier = Decimal("1")
    for unit_re, mult in _WEIGHT_UNIT_MULTIPLIERS:
        if unit_re.search(text):
            multiplier = mult
            break

    match = _WEIGHT_NUMBER_RE.search(text)
    if not match:
        return None
    token = match.group(1).replace(",", "")
    if token.count(".") > 1:
        return None
    try:
        number = Decimal(token)
    except InvalidOperation:
        return None
    return _finish_weight(number * multiplier)


def _finish_weight(kilograms: Decimal) -> Decimal | None:
    if kilograms < 0 or kilograms > _WEIGHT_MAX_KG:
        return None
    return kilograms.quantize(_WEIGHT_QUANTUM)


# ===========================================================================
# 统一出口
# ===========================================================================
@dataclass(slots=True, frozen=True)
class NormalizedValue:
    """一个字段的归一结果，供 compare.py 消费并映射到 comparisons 表。"""

    field: str
    raw: str | None
    blank: bool
    text: str | None = None
    tokens: frozenset[str] = field(default_factory=frozenset)
    country: str | None = None
    code: str | None = None
    count: int | None = None
    kilograms: Decimal | None = None
    parse_error: str | None = None

    @property
    def parsed(self) -> dict[str, Any]:
        """写入 comparisons.si_parsed / bl_parsed 的 jsonb。"""
        payload: dict[str, Any] = {}
        if self.text is not None:
            payload["text"] = self.text
        if self.tokens:
            payload["tokens"] = sorted(self.tokens)
        if self.count is not None:
            payload["count"] = self.count
        if self.kilograms is not None:
            payload["kg"] = str(self.kilograms)
        if self.code:
            payload["code"] = self.code
        if self.country:
            payload["country"] = self.country
        return payload


def normalize_field(field_name: str, value: Any) -> NormalizedValue:
    """按字段分派硬归一。任何异常都被吞掉并标记 parse_error（比对层据此升级人审）。"""
    raw_text = None if value is None else to_text(value)
    blank = is_blank(value)
    if field_name not in COMPARE_FIELDS:
        return NormalizedValue(field=field_name, raw=raw_text, blank=blank,
                               parse_error=f"unknown_field:{field_name}")
    if blank:
        return NormalizedValue(field=field_name, raw=raw_text, blank=True)
    try:
        if field_name in ENTITY_FIELDS:
            text = norm_entity(value)
            if text is None:
                return NormalizedValue(field=field_name, raw=raw_text, blank=True)
            return NormalizedValue(field=field_name, raw=raw_text, blank=False,
                                   text=text, tokens=entity_tokens(text))
        if field_name in PORT_FIELDS:
            parts = norm_port(value)
            if parts is None or parts.is_empty:
                return NormalizedValue(field=field_name, raw=raw_text, blank=True)
            return NormalizedValue(field=field_name, raw=raw_text, blank=False,
                                   text=parts.place, tokens=parts.tokens,
                                   country=parts.country, code=parts.code)
        if field_name == "container_count":
            count = norm_count(value)
            if count is None:
                return NormalizedValue(field=field_name, raw=raw_text, blank=False,
                                       parse_error="count_unparsed")
            return NormalizedValue(field=field_name, raw=raw_text, blank=False, count=count)
        if field_name == "gross_weight_kg":
            kilograms = norm_weight(value)
            if kilograms is None:
                return NormalizedValue(field=field_name, raw=raw_text, blank=False,
                                       parse_error="weight_unparsed")
            return NormalizedValue(field=field_name, raw=raw_text, blank=False,
                                   kilograms=kilograms)
    except Exception as exc:  # noqa: BLE001 —— 归一失败必须是"不可判定"，不是崩溃
        return NormalizedValue(field=field_name, raw=raw_text, blank=False,
                               parse_error=f"{type(exc).__name__}:{exc}")
    return NormalizedValue(field=field_name, raw=raw_text, blank=blank,
                           parse_error="unreachable")


def normalize_document(values: Mapping[str, Any]) -> dict[str, NormalizedValue]:
    return {field: normalize_field(field, values.get(field)) for field in COMPARE_FIELDS}


if __name__ == "__main__":  # pragma: no cover —— 快速自检
    assert norm_entity("SHIPPER: APRIL FAR EAST (M) SDN BHD") == "APRIL FAR EAST (M) SDN BHD"
    assert norm_entity("To the Order of: UAB NOVAKOPA\n  SAVANORIU PR. 187") == "UAB NOVAKOPA"
    assert norm_entity("Consignee (Non-Negotiable): EAST BRIGHT FZ-LLC") == "EAST BRIGHT FZ-LLC"
    assert norm_entity("MOORIM SP CO., LTD") == norm_entity("MOORIM SP CO. LTD")
    assert norm_entity("BALL & DOGGETT AUSTRALIA PTY LTD") == "BALL AND DOGGETT AUSTRALIA PTY LTD"
    assert norm_entity("KPP-ANTALIS (SINGAPORE) PTE. LTD.") == "KPP-ANTALIS (SINGAPORE) PTE LTD"
    assert norm_entity("ORIENT LINKS CO (LLC)") == "ORIENT LINKS CO (LLC)"
    assert norm_entity("APRIL FAR EAST (M) SDN BHD") == "APRIL FAR EAST (M) SDN BHD"
    assert is_blank("???") and is_blank("_______") and is_blank("____MT") and is_blank("TBA")
    assert not is_blank("0") and not is_blank(0)

    pol = norm_port("NANTONG, CHINA (CNNTG)")
    # 注意 country 是 "CHINA" 而不是 "CN" —— _COUNTRY_ALIAS 只统一**同一国家的不同写法**
    # （USA/U.S.A./UNITED STATES → US），不做全量 ISO-2 转换。两侧写法一致就能对齐，
    # 而 country 只是辅助信号（真正的判定靠 place + code_conflict）。
    assert pol and (pol.place, pol.city, pol.country, pol.code) == \
        ("NANTONG, CHINA", "NANTONG", "CHINA", "CNNTG"), pol
    west = norm_port("PORT KLANG (WESTPORT), MALAYSIA (MYPKG)")
    assert west and west.place == "PORT KLANG (WESTPORT), MALAYSIA" and west.code == "MYPKG", west
    assert norm_port("MOMBASA, KENYA (KEMBA)").code == "KEMBA"
    # (KENYA) 不是 code，(WESTPORT) 也不是 code
    assert norm_port("RUGAO/NANTONG/SHANGHAI, CHINA (CNSHA)").place == \
        "RUGAO/NANTONG/SHANGHAI, CHINA"

    assert norm_count("6 x 40'HC") == 6
    assert norm_count("Total Containers: 6") == 6
    assert norm_count("SIX (6) FORTY FOOT HC") == 6
    assert norm_count("???") is None
    assert norm_weight("131,058 KG") == Decimal("131058.000")
    assert norm_weight("131.058 MT") == Decimal("131058.000")
    assert norm_weight(131058) == Decimal("131058.000")
    assert norm_weight("___MT") is None
    print("normalize.py self-test OK")
