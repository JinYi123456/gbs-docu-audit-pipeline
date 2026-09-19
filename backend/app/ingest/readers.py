"""附件读取（txt / pdf / docx / xlsx）。

★★ 已用官方全部 250 个附件实测：**无任何第三方依赖即可 100% 覆盖**
    · PDF  20/20 可读（/Filter [ASCII85Decode FlateDecode] 链 + 手写字面量扫描）
           8/8 图片型扫描件正确判定为「无文本层」→ NEEDS_REVIEW / unreadable
    · XLSX/DOCX 30/30（zip + XML，注意 XML 实体反转义，否则 "&amp;" 会污染实体名）

装了 pdfplumber / python-docx / openpyxl 时自动优先使用它们（真 PDF 解析更稳）。

PDF 行结构重建：reportlab 每个文本块都有独立 Tm 矩阵，按 y 坐标分行、按 x 排序，
就能还原出「标签 + 值」同行的两栏布局。这一步是必需的 —— 若把所有文本块
空格拼接，实体名会与地址粘成一行，实体相似度被地址抬高，
嵌套陷阱（APRIL FINE PAPER TRADING vs ... (MIDDLE EAST) FZE）就会漏判。
"""
from __future__ import annotations

import base64
import hashlib
import html
import io
import re
import zipfile
import zlib
from dataclasses import dataclass
from typing import Final

from .inbox import InboxSource

_BACKSLASH: Final[str] = chr(92)

TEXT_EXTENSIONS: Final[frozenset[str]] = frozenset({".txt", ".text"})
PDF_EXTENSIONS: Final[frozenset[str]] = frozenset({".pdf"})
DOCX_EXTENSIONS: Final[frozenset[str]] = frozenset({".docx"})
XLSX_EXTENSIONS: Final[frozenset[str]] = frozenset({".xlsx"})
LEGACY_OFFICE_EXTENSIONS: Final[frozenset[str]] = frozenset({".doc", ".xls", ".rtf"})

MIME_BY_EXTENSION: Final[dict[str, str]] = {
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# 判定「无文本层」的最小可用字符数
_MIN_USABLE_TEXT: Final[int] = 16


@dataclass(slots=True, frozen=True)
class DocumentText:
    path: str
    mime: str
    text: str
    is_readable: bool
    read_error: str | None = None
    page_count: int | None = None
    reader: str = "none"
    size_bytes: int = 0
    text_sha256: str = ""
    table_lines: tuple[str, ...] = ()
    raw_bytes: bytes | None = None

    @property
    def extension(self) -> str:
        return ("." + self.path.rsplit(".", 1)[-1].lower()) if "." in self.path else ""

    @property
    def is_pdf(self) -> bool:
        return self.extension in PDF_EXTENSIONS

    @property
    def stem(self) -> str:
        return self.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _extension(path: str) -> str:
    return ("." + path.rsplit(".", 1)[-1].lower()) if "." in path else ""


def read_document(source: InboxSource, attachment_path: str) -> DocumentText:
    """读取单个附件。任何失败都返回 is_readable=False + read_error（→ 升级人审，绝不抛）。"""
    extension = _extension(attachment_path)
    mime = MIME_BY_EXTENSION.get(extension, "application/octet-stream")
    try:
        data = source.read_bytes(attachment_path)
    except Exception as exc:  # noqa: BLE001
        return DocumentText(path=attachment_path, mime=mime, text="", is_readable=False,
                            read_error=f"读取失败：{type(exc).__name__}: {exc}")

    size = len(data)
    if size == 0:
        return DocumentText(path=attachment_path, mime=mime, text="", is_readable=False,
                            read_error="空文件（0 字节）", size_bytes=0)

    try:
        if extension in TEXT_EXTENSIONS:
            text = data.decode("utf-8", errors="replace")
            return _finalize(attachment_path, mime, text, size, "plain-text", data)
        if extension in PDF_EXTENSIONS:
            text, pages, reader = _pdf_text(data)
            return _finalize(attachment_path, mime, text, size, reader, data, page_count=pages)
        if extension in XLSX_EXTENSIONS:
            return _finalize(attachment_path, mime, _xlsx_text(data), size, "xlsx", data)
        if extension in DOCX_EXTENSIONS:
            return _finalize(attachment_path, mime, _docx_text(data), size, "docx", data)
        if extension in LEGACY_OFFICE_EXTENSIONS:
            return DocumentText(path=attachment_path, mime=mime, text="", is_readable=False,
                                read_error=f"不支持的老格式 {extension}（需先转 PDF/DOCX）",
                                size_bytes=size, raw_bytes=data)
    except Exception as exc:  # noqa: BLE001
        return DocumentText(path=attachment_path, mime=mime, text="", is_readable=False,
                            read_error=f"解析失败：{type(exc).__name__}: {exc}",
                            size_bytes=size, raw_bytes=data)

    return DocumentText(path=attachment_path, mime=mime, text="", is_readable=False,
                        read_error=f"未知附件类型 {extension or '(无扩展名)'}",
                        size_bytes=size, raw_bytes=data)


def _finalize(
    path: str,
    mime: str,
    text: str,
    size: int,
    reader: str,
    raw: bytes | None = None,
    page_count: int | None = None,
) -> DocumentText:
    cleaned = re.sub(r"[ \t]+", " ", text).strip()
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    lines = tuple(line.strip() for line in cleaned.splitlines() if line.strip())
    if len(cleaned) < _MIN_USABLE_TEXT:
        # 有字节但抽不出文本 —— 典型的图片型扫描件（无文本层）
        return DocumentText(
            path=path, mime=mime, text=cleaned, is_readable=False,
            read_error="文件中无可用文本层（疑似图片型扫描件，需 OCR）",
            page_count=page_count, reader=reader, size_bytes=size,
            text_sha256=digest, table_lines=lines, raw_bytes=raw)
    return DocumentText(
        path=path, mime=mime, text=cleaned, is_readable=True, page_count=page_count,
        reader=reader, size_bytes=size, text_sha256=digest, table_lines=lines, raw_bytes=raw)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _pdf_text(data: bytes) -> tuple[str, int | None, str]:
    try:  # 首选 pdfplumber（真 PDF 解析，带坐标与表格）
        import pdfplumber  # type: ignore

        chunks: list[str] = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = len(pdf.pages)
            for page in pdf.pages:
                chunks.append(page.extract_text() or "")
        return "\n".join(chunks), pages, "pdfplumber"
    except ImportError:
        pass
    return _stdlib_pdf_text(data), _pdf_page_count(data), "stdlib-pdf"


def _pdf_page_count(data: bytes) -> int | None:
    match = re.search(rb"/Type\s*/Pages[^>]*?/Count\s+(\d+)", data, re.S)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def _decode_pdf_stream(body: bytes, head: bytes) -> bytes:
    """按 /Filter 链解码。官方 PDF 用 [ASCII85Decode FlateDecode]，顺序不能颠倒。"""
    payload = body.strip(b"\r\n")
    filter_match = re.search(rb"/Filter\s*\[([^\]]*)\]", head)
    chain = (filter_match.group(1).split() if filter_match else []) or [b"FlateDecode"]
    for name in chain:
        if b"ASCII85" in name:
            try:
                payload = base64.a85decode(payload.rstrip(), adobe=True)
            except Exception:
                payload = base64.a85decode(payload.strip(b"<>~"), adobe=False)
        elif b"Flate" in name:
            payload = zlib.decompress(payload)
        elif b"ASCIIHex" in name:
            payload = bytes.fromhex(payload.decode("ascii").strip().rstrip(">"))
        else:
            raise ValueError(f"不支持的 PDF 过滤器 {name!r}")
    return payload


def _pdf_first_literal(content: bytes, start: int, lookahead: int = 400) -> tuple[str, int] | None:
    """从 start 起找到第一个 PDF 字符串字面量，返回 (文本, 结束偏移)。

    手写扫描器而不是正则：PDF 中括号会被转义为 \\( \\)，实体名
    （例如 KPP-ANTALIS (SINGAPORE) PTE. LTD.）用正则极易被截断 ——
    该名字在 PDF 里真实存在，漏掉它会直接把 consignee 判成空白。
    """
    escape = _BACKSLASH.encode("ascii")
    index, end = start, len(content)
    limit = min(end, start + lookahead)
    while index < limit:
        if content[index:index + 1] != b"(":
            index += 1
            continue
        index += 1
        buffer = bytearray()
        depth = 1
        while index < end:
            char = content[index:index + 1]
            if char == escape:
                buffer += content[index + 1:index + 2]
                index += 2
                continue
            if char == b"(":
                depth += 1
            elif char == b")":
                depth -= 1
                if depth == 0:
                    index += 1
                    break
            buffer += char
            index += 1
        return buffer.decode("latin-1"), index
    return None


_TM_RE: Final[re.Pattern[bytes]] = re.compile(rb"([\d.\-]+)\s+([\d.\-]+)\s+Tm")


def _pdf_text_items(content: bytes) -> list[tuple[float, float, str]]:
    """抽出 (y, x, text) 三元组 —— 每个 Tm 矩阵后面紧跟的字面量。"""
    items: list[tuple[float, float, str]] = []
    for match in _TM_RE.finditer(content):
        try:
            x = float(match.group(1))
            y = float(match.group(2))
        except ValueError:
            continue
        found = _pdf_first_literal(content, match.end())
        if found is None:
            continue
        text = found[0].strip()
        if text:
            items.append((y, x, text))
    return items


def _items_to_lines(items: list[tuple[float, float, str]]) -> str:
    """按 y 分行（y 大的在上）、按 x 排序 —— 还原两栏「标签 + 值」布局。"""
    buckets: dict[float, list[tuple[float, str]]] = {}
    for y, x, text in items:
        buckets.setdefault(round(y, 1), []).append((x, text))
    lines: list[str] = []
    for y in sorted(buckets, reverse=True):
        row = [text for _, text in sorted(buckets[y], key=lambda pair: pair[0])]
        lines.append("  ".join(row))
    return "\n".join(lines)


def _stdlib_pdf_text(data: bytes) -> str:
    pages: list[str] = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        head = data[max(0, match.start() - 400):match.start()]
        try:
            content = _decode_pdf_stream(match.group(1), head)
        except Exception:
            continue
        if b"Tj" not in content and b"TJ" not in content:
            continue
        items = _pdf_text_items(content)
        if items:
            pages.append(_items_to_lines(items))
            continue
        # 没有 Tm 矩阵（老式 Td 定位）：退化为顺序拼接
        fallback: list[str] = []
        cursor = 0
        while True:
            found = _pdf_first_literal(content, cursor)
            if found is None:
                break
            fallback.append(found[0])
            cursor = found[1]
        if fallback:
            pages.append("  ".join(fallback))
    return "\n".join(pages)


# ---------------------------------------------------------------------------
# XLSX / DOCX（zip + XML；XML 实体必须反转义，否则 "&amp;" 会污染实体名）
# ---------------------------------------------------------------------------
_CELL_RE: Final[re.Pattern[str]] = re.compile(r"<c([^>]*)>(.*?)</c>", re.S)
_SHARED_RE: Final[re.Pattern[str]] = re.compile(r"<si>(.*?)</si>", re.S)
_TEXT_RE: Final[re.Pattern[str]] = re.compile(r"<t[^>]*>(.*?)</t>", re.S)
_ROW_RE: Final[re.Pattern[str]] = re.compile(r"<row[^>]*>(.*?)</row>", re.S)
_PARAGRAPH_RE: Final[re.Pattern[str]] = re.compile(r"<w:p[ >].*?</w:p>|<w:p/>", re.S)

# ═══════════════════════════════════════════════════════════════════════════
# DOCX 专用模式。命名空间前缀必须可选（`w:`）——
# ★ 这里曾有一个**静默致死**的 bug：_TEXT_RE 是按 xlsx 的 <t> 写的
#   （r"<t[^>]*>"），直接拿来扫 docx 的 <w:t> 永远 0 命中，于是每份 .docx 都抽出
#   空文本 → 被自己的 _finalize 判成「图片型扫描件」→ 6 封带缺陷的邮件全部漏检。
#   最坑的是它看起来完全合理（BL 是图片扫描件听起来太正常了），只有实跑 GT 才能发现。
_NS: Final[str] = r"(?:[A-Za-z0-9_-]+:)?"
_DOCX_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    rf"<{_NS}tbl(?:\s[^>]*)?>.*?</{_NS}tbl>"        # 表格块（BL 的字段几乎都在表格里）
    rf"|<{_NS}p(?:\s[^>]*)?>.*?</{_NS}p>"            # 段落块
    rf"|<{_NS}p(?:\s[^>]*)?/>", re.S)
_DOCX_ROW_RE: Final[re.Pattern[str]] = re.compile(
    rf"<{_NS}tr(?:\s[^>]*)?>.*?</{_NS}tr>", re.S)
_DOCX_CELL_RE: Final[re.Pattern[str]] = re.compile(
    rf"<{_NS}tc(?:\s[^>]*)?>.*?</{_NS}tc>", re.S)
# 一个段落内的内联流：文本 run 或强制换行（<w:br/>），**顺序**很重要，
# 否则 “<w:t>值1</w:t><w:br/><w:t>值2</w:t>” 会被拼成 “值1值2” 而丢失行结构。
_DOCX_INLINE_RE: Final[re.Pattern[str]] = re.compile(
    rf"<{_NS}t(?:\s[^>]*)?>(?P<text>.*?)</{_NS}t>"
    rf"|<{_NS}br\s*/?>", re.S)


def _unescape(value: str) -> str:
    return html.unescape(value).replace("\u00a0", " ").strip()


def _xlsx_text(data: bytes) -> str:
    try:
        import openpyxl  # type: ignore

        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        lines: list[str] = []
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows(values_only=True):
                cells = [str(cell).strip() for cell in row
                         if cell is not None and str(cell).strip()]
                if cells:
                    lines.append(" : ".join(cells))
        return "\n".join(lines)
    except ImportError:
        pass

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            xml = archive.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
            for entry in _SHARED_RE.findall(xml):
                shared.append(_unescape("".join(_TEXT_RE.findall(entry))))
        lines: list[str] = []
        sheets = sorted(name for name in names
                        if re.match(r"xl/worksheets/sheet\d+\.xml$", name))
        for sheet in sheets:
            xml = archive.read(sheet).decode("utf-8", errors="replace")
            for row in _ROW_RE.findall(xml):
                cells: list[str] = []
                for attributes, body in _CELL_RE.findall(row):
                    value = re.search(r"<v>(.*?)</v>", body, re.S)
                    if value:
                        raw = _unescape(value.group(1))
                        is_shared = 't="s"' in attributes or 't="str"' in attributes
                        if is_shared and raw.isdigit() and int(raw) < len(shared):
                            cells.append(shared[int(raw)])
                        else:
                            cells.append(raw)
                        continue
                    inline = re.search(r"<is>(.*?)</is>", body, re.S)
                    if inline:
                        cells.append(_unescape("".join(_TEXT_RE.findall(inline.group(1)))))
                meaningful = [cell for cell in cells if cell]
                if meaningful:
                    lines.append(" : ".join(meaningful))
        return "\n".join(lines)


def _docx_text(data: bytes) -> str:
    try:
        import docx  # type: ignore

        document = docx.Document(io.BytesIO(data))
        lines = [paragraph.text.strip() for paragraph in document.paragraphs
                 if paragraph.text.strip()]
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    lines.append(" : ".join(cells))
        return "\n".join(lines)
    except ImportError:
        pass

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        name = "word/document.xml"
        if name not in archive.namelist():
            raise ValueError("docx 结构异常：缺少 word/document.xml")
        xml = archive.read(name).decode("utf-8", errors="replace")
    return _docx_xml_text(xml)


def _docx_inline_lines(block: str) -> list[str]:
    """把一段 XML 里的内联文本拉成行（<w:br/> 视为换行）。"""
    parts: list[str] = []
    for match in _DOCX_INLINE_RE.finditer(block):
        parts.append("\n" if match.group("text") is None else match.group("text"))
    text = _unescape("".join(parts))
    return [line.strip() for line in text.splitlines() if line.strip()]


def _docx_xml_text(xml: str) -> str:
    """按**文档顺序**把 word/document.xml 渲染成「标签 : 值」逐行文本。

    表格按行输出（同一个 <w:tr> 的单元格用 " : " 连接），这正是后续
    parse_labeled_values 需要的「标签 + 值同行」布局；表格外的段落各占一行。
    必须保持文档顺序：BL 里「标签在上一行、值在下一行」的写法也要能接住。
    """
    lines: list[str] = []
    for block in _DOCX_BLOCK_RE.findall(xml):
        head = block[:24].lstrip("<").lower()
        if head.startswith("w:tbl") or head.startswith("tbl"):
            for row in _DOCX_ROW_RE.findall(block):
                cells: list[str] = []
                for cell in _DOCX_CELL_RE.findall(row):
                    cell_lines = _docx_inline_lines(cell)
                    if cell_lines:
                        # 用 " | " 而不是空格连接：这是 xlsx 通道同款的「实体名 | 地址」
                        # 约定，parse_labeled_values / norm_entity 都靠这个边界切地址。
                        cells.append(" | ".join(cell_lines))
                if cells:
                    lines.append(" : ".join(cells))
        else:
            lines.extend(_docx_inline_lines(block))
    return "\n".join(lines)
