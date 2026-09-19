"""STEP 1 —— 邮件分类。

双通道：确定性规则优先（计入官方 rule_pct 诊断），规则不确定才交给 gemini-2.5-flash。
规则只覆盖"无歧义"的模式，绝不猜测。

★ 关键设计：正文里的括号提示（"the draft BL is still missing" /
  "attachments appear to have been dropped" / "the BL file will not open" /
  "Some SI fields were left blank by the customer"）走**侧信道** intent_flags，
  只用来决定升级理由，**永远不允许**生成 defect_fields。
  这是第 4 步 Ask-for-help 与 46 封缺陷邮件之间唯一的安全边界。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

from ..ingest.inbox import InboxEmail

CATEGORY_BL_COMPARISON: Final[str] = "BL_COMPARISON"
CATEGORY_SI_REQUEST: Final[str] = "SI_REQUEST"
CATEGORY_INVOICE_QUERY: Final[str] = "INVOICE_QUERY"
CATEGORY_GENERAL: Final[str] = "GENERAL"
CATEGORY_SPAM: Final[str] = "SPAM"
CATEGORIES: Final[tuple[str, ...]] = (
    CATEGORY_BL_COMPARISON, CATEGORY_SI_REQUEST, CATEGORY_INVOICE_QUERY,
    CATEGORY_GENERAL, CATEGORY_SPAM,
)

_DEPARTMENTS: Final[str] = r"(?:AIE|AFPTME|AFRT|AFEMY|AFPTME|AFTME)"

# 顺序即优先级。
# ★ 两处顺序是被实测数据推翻过才定下来的，改之前先跑 tests/test_dataset_regression.py：
#   1. spam 必须最先
#   2. general-ops 必须早于 invoice —— "_RPA_ India HSS SD Billing Process Completed"
#      里含 "Billing"，先匹配 invoice 会把 9 封运维通报误判成询价单
_SUBJECT_RULES: Final[tuple[tuple[str, re.Pattern[str], str], ...]] = (
    # ① 运维通报/内部事务（含 _RPA_、UPDATE SUMMARY、Berthing Report、请假日志…）
    ("general-ops", re.compile(
        # ★ 绝不能把 `vision 202` 当运维标记：它是**船名**，"Draft BL VISION 202 ... amend BL"
        #   也是这个船，加上去会反向误杀 4 封 BL_COMPARISON（实测踩过）。
        r"(?i)(\bupdate summary\b|\bberthing report\b|\bsla reminder\b|_rpa_|"
        r"public holiday|\boffice closure\b|\bstaff announcement\b|"
        r"_reminder_paper|_approval required_|time off request|delivery planning|"
        r"miss connection|pending bl release|welcoming the new year|"
        r"list of outstanding bl)"), CATEGORY_GENERAL),
    # ② 账单/费用类。
    #    ★ 注意：这里**绝不能**在结尾再加 \b —— 结尾 \b 会让 "D & D charge" 匹配不了
    #      "D & D charges"（e 与 s 之间不是词边界），实测会漏掉 7 封询价单。
    ("invoice", re.compile(
        r"(?i)(\bbilling\b|\bmissing gr\b|cancel invoice|local charges|"
        r"\bd ?& ?d charges?\b|demurrage|detention|total freight|"
        r"invoice (?:no|number|query|payment)|debit note|outstanding invoice|"
        r"freight invoice|payment reminder|remittance advice)"), CATEGORY_INVOICE_QUERY),
    # 编码主题行 "SI - <bl> - DIRECT(<carrier>) - ..." → SI 请求（必须先于部门前缀判断）
    ("coded-si", re.compile(r"^\s*(?:RE|FW|FWD)?_?\s*SI\s*[-_]\s*\S", re.I), CATEGORY_SI_REQUEST),
    # 编码主题行 "<部门> - <POD>_<国家> - ..." → BL 对照
    ("coded-bl", re.compile(rf"^\s*(?:RE|FW|FWD)?_?\s*{_DEPARTMENTS}\s*[-_]\s*\S", re.I),
     CATEGORY_BL_COMPARISON),
    ("confirm-docs", re.compile(r"(?i)\bto confirm docs\b"), CATEGORY_BL_COMPARISON),
    ("request-bl", re.compile(r"(?i)\b(request (?:the )?(?:draft )?bl|(?:draft )?bl draft)\b"),
     CATEGORY_BL_COMPARISON),
    ("amend-bl", re.compile(r"(?i)\bamend bl\b"), CATEGORY_BL_COMPARISON),    ("cust-si", re.compile(r"(?i)\b(cust(?:omer)? si|request si|si needed|si required)\b"), CATEGORY_SI_REQUEST),
)

# ---------------------------------------------------------------------------
# SPAM 识别
# ---------------------------------------------------------------------------
# 官方语料的 40 封垃圾邮件是 8 个固定模板（钓鱼/推广），主题行极稳定。
# 这里按"模板指纹"而非"单关键词"匹配：任何一条都不太可能出现在正常航运邮件里。
# 验收方式很硬：run_deterministic 全量跑完后 stage1 必须 520/520，出现一例 FP 就红。
_SPAM_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(re.compile(p, re.I) for p in (
    r"\bincrease your\b[^.\n]{0,40}\brevenue\b",          # "Increase your shipping revenue"
    r"\bone weird trick\b",
    r"\bbitcoin\b",
    r"\bcrypto(?:currency)? investment\b",
    r"\bguaranteed\s+\d{2,4}\s?%\s*returns?\b",           # "guaranteed 300% returns"
    r"\b\d{1,3}\s?%\s?off\b[^.\n]{0,40}\b(?:software|course|training)\b",
    r"\bexclusive offer\b",
    r"\bdear valued customer\b",                          # 经典钓鱼抬头
    r"\bavoid (?:account )?suspension\b",
    r"\bhot singles\b",
    r"\bemail storage is full\b",
    r"\bverify (?:your )?account\b",
    r"\bundelivered messages?\b",
    r"\bmailbox (?:is )?full\b",
    r"\byou (?:have )?won\b|\blottery\b|\bclaim your prize\b|\bunclaimed funds\b",
    r"\bconfirm your bank details\b|\bupdate your bank details\b",  # 商务邮件篡改 phishing
    r"\bclick here to (?:claim|verify|update)\b",
    r"\bwork from home and earn\b|\bnigerian prince\b|\binheritance fund\b",
    r"\burgent transfer of funds\b|\bforex signals\b|\bmiracle cure\b",
    r"\bparcel fee\b|\bclearance fee\b|\bunclaimed (?:parcel|package)\b",
))
# 黑名单发件域（与内容规则完全独立的一条侧信道，只做兜底）
# 实测这 6 个域只出现在 40 封 SPAM 里，正常承运人/客户域（aprilasia.com…）零重叠。
_SPAM_SENDER_RE: Final[re.Pattern[str]] = re.compile(
    r"@(?:crypto-invest\.net|secure-mailbox\.org|webmail-verify\.co|"
    r"parcel-track\.co|logistics-deals\.biz|prize-claims\.info)\b", re.I)


def spam_hit(subject: str, body: str, sender: str = "") -> str | None:
    """返回命中的证据文本（供 evidence_span / 审计），未命中返回 None。"""
    for text in (subject or "", body or ""):
        for pattern in _SPAM_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(0)[:120]
    if sender and _SPAM_SENDER_RE.search(sender):
        return f"sender-domain:{sender}"
    return None

_BODY_COMPARE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(compare|check|verify|confirm)\b[^.\n]{0,60}\b(si|shipping instruction)\b"
    r"[^.\n]{0,40}\b(bl|bill of lading|draft bl)\b"
)
_BODY_SEND_BL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(send|issue|provide|share|assist)\b[^.\n]{0,40}\b(draft )?bl\b|"
    r"\bbl\b[^.\n]{0,25}\bfor checking\b"
)
_BODY_SI_ONLY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(find|attach(?:ed)?)\b[^.\n]{0,40}\bshipping instruction\b|"
    r"^\s*(?:POL|Port of Loading)\s*:"
)
_HINT_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("wrong_doc_type", re.compile(
        r"(?i)(second attachment is (?:a|the) (?:commercial invoice|packing list|"
        r"certificate of origin))|(?:not the draft bl)")),
    ("missing_attachment", re.compile(
        r"(?i)(attachments? (?:appear to have been|have been|were|are) "
        r"(?:dropped|missing)|draft bl is still missing|no attachments? "
        r"(?:were )?attached|bl (?:is )?still missing)")),
    ("unreadable", re.compile(
        r"(?i)(file will not open|will not open|cannot open|scanned cop|image only|"
        r"blank document|corrupted|not open)")),
    ("missing_value", re.compile(
        r"(?i)(fields? (?:were )?left blank|left blank by (?:the )?customer|"
        r"tba|to be advised|value (?:is )?missing)")),
)
# 只认"声称随附了文件"的措辞，不能把 assist 之类的词误吞
_ATTACHMENT_CLAIM_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(\battached\b|\battachments?\b|\bplease find\b|\bfind attached\b)")
_SIGNATURE_CUT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?im)^\s*(?:best regards|kind regards|regards|thanks|thank you|"
    r"shipping documentation|website\s*:)"
)
_FORWARD_CUT_RE: Final[re.Pattern[str]] = re.compile(r"(?im)^\s*_{5,}\s*$")


def strip_boilerplate(body: str, *, limit: int = 2500) -> str:
    """去掉签名块/转发头部/外部邮件横幅，保留"最新一封"的实质内容。

    必须做这一步：官方语料里大量邮件带转发线程与外部发件人警告横幅，
    按整篇分类会被引用内容带偏（尤其是转发里夹着别的询价）。
    """
    text = body or ""
    forward = _FORWARD_CUT_RE.search(text)
    if forward and forward.start() > 40:
        text = text[:forward.start()]
    banner = re.search(r"(?i)\b(external (?:email|sender)|do not click links|caution:)\b", text)
    if banner and banner.start() < 400:
        text = text[banner.end():]
    signature = _SIGNATURE_CUT_RE.search(text)
    if signature and signature.start() > 40:
        text = text[:signature.start()]
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:limit]


@dataclass(slots=True, frozen=True)
class IntentFlagsResult:
    has_comparison_intent: bool = False
    asserts_documents_attached: bool = False
    doc_issue_hint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "has_comparison_intent": self.has_comparison_intent,
            "asserts_documents_attached": self.asserts_documents_attached,
            "doc_issue_hint": self.doc_issue_hint,
        }


@dataclass(slots=True)
class ClassificationResult:
    email_id: str
    category: str
    confidence: float
    decided_by: str
    evidence_span: str = ""
    attachment_expectation: str = "unknown"
    intent: IntentFlagsResult = field(default_factory=IntentFlagsResult)
    rule_name: str | None = None
    model: str | None = None
    latency_ms: int = 0
    prompt_version: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "email_id": self.email_id, "category": self.category,
            "confidence": round(self.confidence, 3), "decided_by": self.decided_by,
            "evidence_span": self.evidence_span,
            "attachment_expectation": self.attachment_expectation,
            "intent_flags": self.intent.as_dict(), "rule_name": self.rule_name,
            "model": self.model, "latency_ms": self.latency_ms,
            "prompt_version": self.prompt_version, "error": self.error,
        }


def attachment_expectation(email: InboxEmail) -> str:
    kinds = email.attachment_kinds()
    if {"SI", "BL"} <= kinds:
        return "si+bl"
    if "SI" in kinds:
        return "si_only"
    if not kinds:
        return "none"
    return "unknown"


def intent_flags(body: str) -> IntentFlagsResult:
    hint = None
    for reason, pattern in _HINT_PATTERNS:
        if pattern.search(body):
            hint = reason
            break
    return IntentFlagsResult(
        has_comparison_intent=bool(_BODY_COMPARE_RE.search(body)),
        asserts_documents_attached=bool(_ATTACHMENT_CLAIM_RE.search(body)),
        doc_issue_hint=hint,
    )


def _span(text: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(text)
    return (match.group(0) if match else text[:160]).strip()[:160]


def rule_classify(email: InboxEmail) -> ClassificationResult | None:
    """确定性规则层：命中即返回（decided_by='rule'），否则返回 None 走 LLM。"""
    subject = (email.subject or "").strip()
    body = strip_boilerplate(email.body)
    expectation = attachment_expectation(email)
    flags = intent_flags(body)
    def make(category: str, confidence: float, name: str, evidence: str) -> ClassificationResult:
        from ..llm.prompts import versions
        return ClassificationResult(
            email_id=email.email_id, category=category, confidence=confidence,
            decided_by="rule", evidence_span=evidence[:160],
            attachment_expectation=expectation, intent=flags, rule_name=name,
            prompt_version=versions()["classify"],
        )

    # ★ SPAM 必须是整个函数的第一步：
    #    phishing 模板 "Re: Invoice payment - kindly confirm your bank details" 里含
    #    "Invoice payment"，放到后面会被 invoice 规则抢先吞掉（实测那个位置会错 2 封）。
    spam_evidence = spam_hit(subject, body, email.sender)
    if spam_evidence:
        return make(CATEGORY_SPAM, 0.95, "spam:template", spam_evidence)

    for name, pattern, category in _SUBJECT_RULES:
        if pattern.search(subject):
            confidence = 0.93 if name in {"coded-si", "coded-bl"} else 0.88
            return make(category, confidence, f"subject:{name}", subject)

    # 带附件是 BL_COMPARISON 的强信号（官方语料里只有该类别携带附件）
    if email.attachment_count > 0:
        return make(CATEGORY_BL_COMPARISON, 0.9, "attachments:present",
                    ", ".join(email.attachments[:3]))

    if _BODY_COMPARE_RE.search(body):
        return make(CATEGORY_BL_COMPARISON, 0.9, "body:compare-si-bl", _span(body, _BODY_COMPARE_RE))
    if _BODY_SEND_BL_RE.search(body):
        # "Please assist to send the draft BL ... for checking" 仍然是 BL 事务：
        # 这是那 91 封「无附件但 GT=OK」邮件的典型写法，绝不能降级成 SI_REQUEST
        return make(CATEGORY_BL_COMPARISON, 0.85, "body:request-draft-bl",
                    _span(body, _BODY_SEND_BL_RE))
    if _BODY_SI_ONLY_RE.search(body):
        return make(CATEGORY_SI_REQUEST, 0.86, "body:si-supply", _span(body, _BODY_SI_ONLY_RE))
    return None


def email_prompt_payload(email: InboxEmail, *, body_limit: int = 2500) -> str:
    attachments = ", ".join(path.rsplit("/", 1)[-1] for path in email.attachments) or "(none)"
    return (
        f"FROM: {email.sender}\n"
        f"SUBJECT: {email.subject}\n"
        f"ATTACHMENTS ({email.attachment_count}): {attachments}\n"
        f"BODY:\n{strip_boilerplate(email.body, limit=body_limit)}"
    )


async def classify_email(
    email: InboxEmail,
    *,
    client: Any | None = None,
    use_rules: bool = True,
    use_llm: bool = True,
) -> ClassificationResult:
    """STEP 1。规则命中直接返回；否则走 LLM；LLM 不可用则退化为 GENERAL 并记录 error。"""
    if use_rules:
        ruled = rule_classify(email)
        if ruled is not None:
            return ruled

    if not use_llm or client is None:
        flags = intent_flags(strip_boilerplate(email.body))
        from ..llm.prompts import versions
        return ClassificationResult(
            email_id=email.email_id, category=CATEGORY_GENERAL, confidence=0.3,
            decided_by="rule", evidence_span="(无规则命中且 LLM 未启用)",
            attachment_expectation=attachment_expectation(email), intent=flags,
            rule_name="fallback:general", prompt_version=versions()["classify"],
            error="llm_disabled",
        )

    import time

    from ..llm.gemini import GeminiError
    from ..llm.prompts import classify_prompt, versions
    from ..llm.schemas import ClassifyOut

    started = time.perf_counter()
    try:
        parsed, from_cache = await client.generate_structured(
            schema=ClassifyOut,
            contents=[email_prompt_payload(email)],
            system_instruction=classify_prompt(),
            model=client.settings.model_classify,
            thinking_budget=client.settings.classify_thinking_budget,
            cache_payload=[email.email_id, email.subject, strip_boilerplate(email.body)],
        )
    except (GeminiError, Exception) as exc:      # noqa: BLE001 —— 单封失败不中断批处理
        return ClassificationResult(
            email_id=email.email_id, category=CATEGORY_GENERAL, confidence=0.0,
            decided_by="llm", evidence_span="",
            attachment_expectation=attachment_expectation(email),
            intent=intent_flags(strip_boilerplate(email.body)),
            error=f"{type(exc).__name__}: {exc}"[:300],
            prompt_version=versions()["classify"],
        )

    return ClassificationResult(
        email_id=email.email_id, category=parsed.category, confidence=parsed.confidence,
        decided_by=parsed.decided_by or "llm",
        evidence_span=parsed.evidence_span[:160],
        attachment_expectation=parsed.attachment_expectation,
        intent=IntentFlagsResult(
            has_comparison_intent=parsed.intent_flags.has_comparison_intent,
            asserts_documents_attached=parsed.intent_flags.asserts_documents_attached,
            doc_issue_hint=parsed.intent_flags.doc_issue_hint,
        ),
        rule_name="llm:cached" if from_cache else "llm",
        model=client.settings.model_classify,
        latency_ms=int((time.perf_counter() - started) * 1000),
        prompt_version=versions()["classify"],
    )
