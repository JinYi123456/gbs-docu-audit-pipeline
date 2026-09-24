"""FastAPI 服务 —— 人审工作台的后端（BFF 的上游）。

启动：`make api` → http://localhost:8000/docs

设计原则：**云端优先、本地兜底**。
  · 配了 Supabase → 读 submission_view / dashboard_view（人工改判自动生效）；
  · 没配 → 退回读跑批快照 eval/report/dashboard.json + 本地改判文件。
于是同一套 API 在「有云」和「断网演示」两种环境下都是可用的，前端无需分支。

安全：service-role key 只在本进程内使用，绝不返回给客户端；
`/api/submission` 与 `/api/review` 是唯一的写/提交出口。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:                       # 缺依赖时给出可执行的提示，而不是一串 ImportError 栈
    from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel, Field
except ModuleNotFoundError as exc:      # pragma: no cover
    raise SystemExit(
        f"缺少依赖 {exc.name}。请先 `pip install -r requirements.txt`，"
        f"或只用前端工作台（它不需要 FastAPI）。") from exc

from .config import (
    DEFAULT_DASHBOARD_PATH, DEFAULT_REPORT_DIR, SUBMISSION_KEYS, gemini_api_key_available,
)
from .dashboard import FIELD_LABELS, REASON_LABELS, load_dashboard
from .db.repo import strip_diagnostics
from .pipeline.normalize import COMPARE_FIELDS

APP_VERSION = "0.1.0"
LOCAL_OVERRIDES_PATH = Path(os.environ.get(
    "MANUAL_OVERRIDES_PATH", str(DEFAULT_REPORT_DIR / "manual_overrides.json")))

app = FastAPI(title="SDOC SI-vs-BL Verification API", version=APP_VERSION,
              description="Ocean-freight document verification: classify → extract → "
                          "compare → human escalation")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in
                   os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",") if origin.strip()],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# 数据源：Supabase 优先，本地快照兜底
# ---------------------------------------------------------------------------
logger = logging.getLogger("sdoc.api")


def _supabase_ready() -> bool:
    from .db.supabase import SupabaseSettings, sdk_available
    return sdk_available() and SupabaseSettings.from_env().configured


def _local_dashboard() -> dict[str, Any]:
    """优先读本地快照；Railway 等无文件环境自动回落到 Supabase 云端审计页。

    云端行与快照同构（email_id/subject/from/attachments/category/status/…），
    因此下游所有列表/详情/队列端点对数据源无感。字段级并排明细依赖附件原文，
    云端容器里没有文件，故置空 —— 详情面板会走它现成的「无可比字段」分支。
    """
    payload = load_dashboard(DEFAULT_DASHBOARD_PATH)
    if payload is not None:
        return payload
    fallback_reason = ""
    if _supabase_ready():
        try:
            cloud = _cloud_emails_page()
            if cloud:
                return {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "summary": {}, "roi": {},
                    "field_labels": FIELD_LABELS, "reason_labels": REASON_LABELS,
                    "emails": [_cloud_row_to_detail(cloud[key]) for key in sorted(cloud)],
                    "source": "supabase",
                }
        except Exception as exc:      # noqa: BLE001 —— 云端失败继续走 503 提示
            fallback_reason = f"{type(exc).__name__}: {exc}"
            logger.warning("云端审计页读取失败，退回 503：%s", fallback_reason)
    raise HTTPException(
        status_code=503,
        detail=("No verification snapshot available yet. Generate one with "
                "`python -m app.dashboard`, or configure the Supabase environment "
                "variables to read the cloud data source instead."
                + (f" [cloud read failed: {fallback_reason}]" if fallback_reason else "")))


def _cloud_emails_page() -> dict[str, dict[str, Any]]:
    """云端审计页的同步取数入口（FastAPI 的 def 路由跑在线程池里，不能 await）。"""
    from .db.repo import fetch_emails_page
    return fetch_emails_page()


def _cloud_effective(row: dict[str, Any]) -> dict[str, Any]:
    """把 emails 表的原始行合并成"展示有效值"（人工改判优先，与 submission_view 同口径）。

    列表 / 详情 / 云端回放三处共用，保证任何一个入口看到的结论都一致。
    manual_* 原始键保留在结果里，供 "decided_by" 与轨迹标注使用。
    """
    manual = row.get("manual_status")
    status = manual or row.get("status") or "OK"
    defect_fields = sorted(set((row.get("manual_defect_fields") if manual
                                else row.get("defect_fields")) or []))
    return {**row,
            "category": row.get("manual_category") or row.get("category") or "GENERAL",
            "status": status,
            "has_defect": status == "MISMATCH" and bool(defect_fields),
            "defect_fields": defect_fields,
            "review_reason": (row.get("manual_review_reason") if manual
                              else row.get("review_reason"))}


def _cloud_row_to_detail(row: dict[str, Any]) -> dict[str, Any]:
    """云端行 → 与本地快照条目同构的详情结构（字段级明细在云端模式置空）。

    列表 / 详情 / _local_dashboard 云端分支三处共用：前端 FieldDiff 等组件
    直接读 `email.fields.length`，缺键就是崩溃 —— 所以云端行必须先补全形状。
    """
    row = _cloud_effective(row)
    return {
        "email_id": row["email_id"],
        "from": row["from"],
        "subject": row["subject"],
        "body": "(email body is not exposed in the cloud audit page)",
        "attachments": row["attachments"],
        "attachment_count": row["attachment_count"],
        "category": row["category"],
        "status": row["status"],
        "has_defect": row["has_defect"],
        "defect_fields": row["defect_fields"],
        "review_reason": row["review_reason"],
        "review_reason_label": REASON_LABELS.get(row["review_reason"] or "", None),
        "decided_by": "manual" if row["manual_status"] else row["decided_by"],
        # 改判标记必须穿过形状转换：列表端点靠它画 "overridden" 徽章
        "overridden": bool(row.get("overridden")),
        "rule_name": None, "category_confidence": None, "body_hint": None,
        "si": None, "bl": None, "fields": [],
        "trace": [], "trace_skipped_agents": [], "trace_total_ms": 0,
    }


def _cloud_email_detail(email_id: str) -> dict[str, Any] | None:
    """云端单封详情：结构对齐本地快照条目，字段级并排明细置空。"""
    if not _supabase_ready():
        return None
    try:
        from .db.repo import fetch_emails_page
        rows = fetch_emails_page(email_ids=[email_id])
    except Exception as exc:      # noqa: BLE001
        logger.warning("云端邮件详情读取失败 %s：%s", email_id, exc)
        return None
    row = rows.get(email_id)
    if row is None:
        return None
    return _cloud_row_to_detail(row)


def _load_overrides() -> dict[str, dict[str, Any]]:
    if not LOCAL_OVERRIDES_PATH.is_file():
        return {}
    try:
        return json.loads(LOCAL_OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_overrides(overrides: dict[str, dict[str, Any]]) -> None:
    LOCAL_OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_OVERRIDES_PATH.write_text(
        json.dumps(overrides, indent=2, ensure_ascii=False), encoding="utf-8")


def _record_from_local(email: dict[str, Any], overrides: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """把本地快照的一条邮件折算成官方 5 键（人工改判优先）。"""
    category = email["category"]
    override = overrides.get(email["email_id"])
    status = override["status"] if override else email["status"]
    if category != "BL_COMPARISON":
        return {"category": category, "status": "OK", "review_reason": None,
                "has_defect": False, "defect_fields": []}
    if status == "MISMATCH":
        fields = override["defect_fields"] if override else email["defect_fields"]
        return {"category": category, "status": status, "review_reason": None,
                "has_defect": True, "defect_fields": sorted(fields)}
    if status == "NEEDS_REVIEW":
        reason = (override["review_reason"] if override else email["review_reason"]) or "missing_value"
        return {"category": category, "status": status, "review_reason": reason,
                "has_defect": False, "defect_fields": []}
    return {"category": category, "status": "OK", "review_reason": None,
            "has_defect": False, "defect_fields": []}


def current_submission() -> dict[str, dict[str, Any]]:
    if _supabase_ready():
        from .db.repo import fetch_submission
        return strip_diagnostics(fetch_submission())
    dashboard = _local_dashboard()
    overrides = _load_overrides()
    return {email["email_id"]: _record_from_local(email, overrides)
            for email in dashboard["emails"]}


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class ReviewRequest(BaseModel):
    email_id: str = Field(..., description="Email ID, e.g. email_004")
    status: str = Field(..., description="OK | MISMATCH | NEEDS_REVIEW")
    defect_fields: list[str] = Field(default_factory=list,
                                     description="Field names must come from the 7 canonical fields")
    review_reason: str | None = None
    note: str | None = None
    reviewer: str | None = None


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.get("/health", summary="Health check (never echoes secrets)")
def health() -> dict[str, Any]:
    from .db.supabase import gateway_status
    return {
        "status": "ok",
        "version": APP_VERSION,
        "source": "supabase" if _supabase_ready() else "local-snapshot",
        "comparison_fields": list(COMPARE_FIELDS),
        "supabase": gateway_status(),
        "snapshot_exists": DEFAULT_DASHBOARD_PATH.is_file(),
        # 有密钥也只返回 set/missing，绝不回显
        "gemini_key": "set" if os.environ.get("GEMINI_API_KEY") else "missing",
    }


# ---------------------------------------------------------------------------
# AI 侧状态：给前端与答辩一个「AI 真的接好了」的可见证据
# ---------------------------------------------------------------------------
@app.get("/api/ai-status", summary="Model channel configuration and probe results (no calls, no cost)")
def ai_status() -> dict[str, Any]:
    """读模型探活缓存，**不发任何 API 调用**。

    为什么值得有这个端点：评委问“你们的 AI 到底接没接”时，
    比起翻代码，直接打开这个 JSON 更有说服力 —— 它给出：
      · 当前实际使用的分类/抽取型号（不是写死的那种，是探活命中的）
      · 每个候选型号的探活结果（哪个 404、哪个 429 配额为 0）
      · 提示词版本、思考预算、并发与超时
    """
    payload: dict[str, Any] = {
        "key_configured": gemini_api_key_available(),
        "sdk_available": _gemini_sdk_available(),
    }
    if not payload["key_configured"]:
        payload["detail"] = ("No GEMINI_API_KEY configured — the pipeline falls back to the "
                            "deterministic rule path")
        return payload
    try:
        from .llm.gemini import GeminiSettings
        from .llm.prompts import versions

        settings = GeminiSettings.from_env()
        payload.update({
            "configured_models": {
                "classify": settings.model_classify,
                "extract": settings.model_extract,
                "fallback": settings.model_fallback or None,
            },
            "candidate_chains": {
                "classify": list(settings.classify_candidates),
                "extract": list(settings.extract_candidates),
            },
            "thinking_budget": {
                "classify": settings.classify_thinking_budget,
                "extract": settings.extract_thinking_budget,
            },
            "concurrency": settings.concurrency,
            "request_timeout_s": settings.request_timeout_s,
            "max_output_tokens": settings.max_output_tokens,
            "cache_enabled": settings.cache_enabled,
            "prompt_version": versions()["classify"],
        })
        # 探活结果：只读缓存文件，没有任何网络开销
        from .llm.gemini import LLMCache, PLAN_CACHE_FILE
        plan_file = LLMCache(settings.cache_dir).directory / PLAN_CACHE_FILE
        if plan_file.is_file():
            payload["resolved"] = json.loads(plan_file.read_text(encoding="utf-8"))
        else:
            payload["resolved"] = {"detail": "Not probed yet — run the pipeline once to "
                                            "populate the model cache"}
    except Exception as exc:      # noqa: BLE001 —— 状态端点绝不能自己 500
        payload["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return payload


def _gemini_sdk_available() -> bool:
    try:
        import google.genai  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


# ---------------------------------------------------------------------------
# 「上传即核对」：用户拖文件进来，实时跑完整链路
# ---------------------------------------------------------------------------
@app.post("/api/verify", summary="On-demand audit: upload SI/draft BL and verify in real time")
async def verify_upload(
    files: list[UploadFile] = File(..., description="SI / draft BL files (PDF, DOCX, XLSX, TXT)"),
    subject: str = Form("", description="Optional email subject (drives routing and side-channel evidence)"),
    body: str = Form("", description="Optional email body (decides whether blanks are defects)"),
    sender: str = Form("uploader@local"),
    use_llm: bool = Form(True, description="Allow the multimodal model channel (falls back to rules without a key)"),
    persist_cloud: bool = Form(True, description="Journal this run to the cloud audit trail"),
) -> dict[str, Any]:
    from .upload import UploadError, UploadedFile, verify_uploaded_documents

    raw: list[tuple[str, bytes]] = []
    for item in files:
        data = await item.read()
        raw.append((item.filename or "upload.bin", data))
    if not raw:
        raise HTTPException(status_code=422, detail="No file received.")

    # 文件名里带角色时直接采信（前端按按钮分开传）；否则按 _SI/_BL 或顺序推断
    uploads = [UploadedFile(name=name, data=data, role=_role_from_name(name))
               for name, data in raw]
    llm_allowed = bool(use_llm and gemini_api_key_available())
    client = None
    if llm_allowed:
        try:
            from .llm.gemini import GeminiSettings, get_client
            client = get_client(GeminiSettings.from_env())
        except Exception as exc:      # noqa: BLE001 —— 客户端建不起来不该让上传失败
            llm_allowed = False
            client = None
            detail = f"Gemini 客户端不可用，已回退规则通道：{type(exc).__name__}: {exc}"[:200]
        else:
            detail = None
    else:
        detail = "未配置 GEMINI_API_KEY —— 已回退确定性规则通道（结果同样可提交）"

    try:
        record = await verify_uploaded_documents(
            uploads, subject=subject, body=body, sender=sender,
            use_llm=llm_allowed, client=client, persist_cloud=persist_cloud)
    except UploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:      # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"Audit failed: {type(exc).__name__}: {exc}"[:300]) from exc

    return {
        "record": record,
        "ai": {
            "requested": bool(use_llm),
            "used": bool(record.get("extractor", {}).get("llm_used")),
            "model": record.get("extractor", {}).get("model") or None,
            "mode": record.get("extractor", {}).get("mode"),
            "detail": detail,
        },
        "storage": record.get("storage", {}),
    }


def _role_from_name(name: str) -> str:
    """从文件名里认出角色（`xxx_SI.pdf` / `SI_xxx.pdf`），认不出就交给顺序推断。"""
    stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0].upper()
    if stem.endswith("_SI") or stem.startswith("SI_"):
        return "SI"
    if stem.endswith("_BL") or stem.startswith("BL_"):
        return "BL"
    return ""


@app.get("/api/uploads", summary="Recent on-demand audits from the cloud audit trail")
def uploads(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    if not _supabase_ready():
        return {"source": "none", "items": [],
                "detail": "Supabase 未配置 —— 上传仍可用，只是不留云端留痕"}
    from .db.repo import list_upload_runs, upload_stats
    try:
        return {"source": "supabase", "items": list_upload_runs(limit=limit),
                "stats": upload_stats()}
    except Exception as exc:      # noqa: BLE001 —— 云端读不到不该让页面崩
        return {"source": "error", "items": [],
                "detail": f"{type(exc).__name__}: {exc}"[:200]}


@app.get("/api/summary", summary="KPI summary and GBS ROI for the console")
def summary() -> dict[str, Any]:
    if _supabase_ready():
        from .db.supabase import get_gateway
        gateway = get_gateway()
        rows = gateway.run(lambda: gateway.table("pipeline_stats").select("*").limit(1).execute(),
                           label="pipeline_stats").data or []
        submission = current_submission()
        return {"source": "supabase", "stats": rows[0] if rows else {},
                "submitted": len(submission)}
    dashboard = _local_dashboard()
    return {"source": "local-snapshot", "generated_at": dashboard["generated_at"],
            "stats": dashboard["summary"], "roi": dashboard.get("roi")}


# ---------------------------------------------------------------------------
# 多 Agent：花名册 + 单封实时轨迹（Pitch 现场"你们的多 Agent 在哪"的答案）
# ---------------------------------------------------------------------------
@app.get("/api/agents", summary="Multi-agent roster and responsibilities")
def agents_roster() -> dict[str, Any]:
    from .agents.escalation_judge import EscalationJudgeAgent
    from .agents.extractor import ExtractorAgent
    from .agents.orchestrator import VerificationOrchestrator
    from .agents.triage import TriageAgent
    from .agents.verifier import CrossVerifierAgent

    orchestrator = VerificationOrchestrator([
        TriageAgent(), ExtractorAgent(), CrossVerifierAgent(), EscalationJudgeAgent()])
    return {
        "pipeline": ["Triage", "Extract", "CrossVerify", "Escalate"],
        "agents": orchestrator.describe(),
        "guardrails": {
            "llm_position": "感知层可用概率模型；裁决层（defect_fields 集合）必须确定性",
            "text_similarity_threshold": 0.94,
            "weight_tolerance": "±10kg 或 0.2%（取宽者）",
        },
    }


@app.get("/api/agents/trace/{email_id}",
          summary="Re-run the multi-agent pipeline for one email and return the real trace")
def agents_trace(email_id: str, use_llm: bool = Query(False),
                 include_llm_detail: bool = Query(True)) -> dict[str, Any]:
    """**现算**（不是读快照）：同一个编排器、同一批 Agent，返回真实耗时与证据。

    默认走确定性通道（毫秒级，不发 API 调用）；`use_llm=true` 时走真正的
    Gemini 多模态抽取（会产生 token 开销，仅在答辩演示需要时打开）。
    返回里带 `matches_submission`，用来现场自证"轨迹与评分产物一致"。
    """
    import asyncio

    from .agents.orchestrator import verify_email
    from .config import resolve_data_dir
    from .ingest.inbox import InboxSource

    source = InboxSource("data", data_dir=resolve_data_dir(None))
    target = next((email for email in source.emails() if email.email_id == email_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No such email: {email_id}")

    client = None
    llm_allowed = bool(use_llm and gemini_api_key_available())
    if llm_allowed:
        try:
            from .llm.gemini import GeminiSettings, get_client
            client = get_client(GeminiSettings.from_env())
        except Exception as exc:      # noqa: BLE001 —— 建不起客户端就退回规则通道
            llm_allowed = False
            client = None
            note = f"Gemini 不可用，已回退规则通道：{type(exc).__name__}"
        else:
            note = None
    else:
        note = "确定性通道（未调用生成式模型）" if not use_llm else "未配置 GEMINI_API_KEY"

    try:
        result = asyncio.run(verify_email(target, source=source, client=client,
                                          use_llm=llm_allowed))
    except Exception as exc:      # noqa: BLE001
        raise HTTPException(status_code=500,
                            detail=f"Orchestration failed: {type(exc).__name__}: {exc}"[:300]) from exc

    payload = result.as_dict()
    payload["email_id"] = email_id
    payload["subject"] = target.subject
    payload["channel_note"] = note
    payload["llm_requested"] = bool(use_llm)
    payload["llm_used"] = bool(llm_allowed)
    try:      # 与提交产物对账：现场自证"我看到的就是拿去打分的那个"
        submitted = current_submission().get(email_id, {})
        payload["matches_submission"] = (
            submitted.get("status") == payload["record"]["status"]
            and sorted(submitted.get("defect_fields") or [])
            == sorted(payload["record"]["defect_fields"]))
    except Exception:      # noqa: BLE001 —— 对账失败不影响轨迹展示
        payload["matches_submission"] = None
    if not include_llm_detail:
        for step in payload["trace"]["steps"]:
            step.pop("evidence", None)
    return payload


# ---------------------------------------------------------------------------
# 「实时拉取邮件」：Demo 视频里那个会闪的按钮背后是真跑一遍链路
# ---------------------------------------------------------------------------
@app.post("/api/stream/pull", summary="Fetch the next batch of GBS mail and audit it live")
async def stream_pull(
    batch_size: int = Query(6, ge=1, le=24),
    reset: bool = Query(False, description="Reset the pool cursor and start from the top"),
    use_llm: bool = Query(False, description="Use the multimodal channel (slower, costs tokens; off by default)"),
) -> dict[str, Any]:
    """每点一次取下一批（环状游标），返回逐封结论 + Agent 轨迹。

    ★ 只读不写：拉取结果**不回写**跑批产物，520 键的提交集不会被现场演示弄脏。
    """
    from .config import resolve_data_dir
    from .ingest.inbox import InboxSource
    from .stream import pull_once, reset_cursor

    client = None
    llm_allowed = bool(use_llm and gemini_api_key_available())
    if llm_allowed:
        try:
            from .llm.gemini import GeminiSettings, get_client
            client = get_client(GeminiSettings.from_env())
        except Exception:      # noqa: BLE001 —— 拉取演示绝不因 LLM 建连失败而 500
            llm_allowed = False
            client = None

    source = InboxSource("data", data_dir=resolve_data_dir(None))
    try:
        source.emails()
    except Exception as exc:      # noqa: BLE001 —— 云端容器没有 data/inbox，回落云端回放
        logger.warning("本地邮件源不可用（%s），stream/pull 切换云端回放模式", type(exc).__name__)
        if not _supabase_ready():
            raise HTTPException(
                status_code=503,
                detail="Live pull needs either the bundled data/inbox on disk or Supabase "
                       "environment variables; neither is available in this environment.") from exc
        if reset:
            reset_cursor()
        return await _cloud_pull(batch_size=batch_size)

    if reset:
        reset_cursor()
    batch = await pull_once(source, batch_size=batch_size, client=client, use_llm=llm_allowed)
    payload = batch.as_dict()
    payload["channel"] = "gemini" if llm_allowed else "deterministic"
    payload["read_only"] = True
    return payload


async def _cloud_pull(*, batch_size: int) -> dict[str, Any]:
    """云端回放：真实邮件池来自 Supabase emails 表，逐封结论是**已归档的权威产物**。

    而且云端行本身就带人工改判字段（manual_*），所以回放自动展示改判后的最终结论
    —— 演示改判闭环时，公网上的流式面板会即时反映人审结果。

    红线约束与本地 pull_once 一致：只读不写、不重跑管线（容器里没有附件字节，
    重跑只会产出误导性的 NEEDS_REVIEW）。游标复用 stream.StreamCursor，
    环状取片语义与本地模式逐字一致。
    """
    import asyncio

    from .agents.base import AgentStep, ROLE_JUDGE, ROLE_TRIAGE, ROLE_VERIFIER
    from .db.repo import fetch_emails_page
    from .stream import CURSOR, StreamItem

    try:
        rows = await asyncio.to_thread(fetch_emails_page)
    except Exception as exc:      # noqa: BLE001 —— 把真实异常带给调用方，而不是裸 500
        raise HTTPException(
            status_code=503,
            detail=f"Cloud replay failed: {type(exc).__name__}: {exc}") from exc
    if not rows:
        raise HTTPException(status_code=503, detail="Supabase returned no emails to replay.")

    # 池子与本地口径一致：有附件的 BL 对照任务，按 email_id 稳定排序
    pool_ids = sorted(
        email_id for email_id, row in rows.items()
        if row["attachment_count"] > 0 and row["category"] == "BL_COMPARISON"
    )
    if not pool_ids:
        raise HTTPException(status_code=503, detail="No comparable BL emails in the cloud pool.")

    size = max(1, min(int(batch_size), 24))
    pool_size = len(pool_ids)
    start = CURSOR.advance(size, pool_size)
    # 环状取片：模数索引实现，与本地 pull_once 的环绕语义一致。
    # ★ 绝不能用 list(cycle(pool_ids)) —— cycle 是无限迭代器，物化成列表会
    #   直接把容器内存打爆（Railway 上表现为边缘 502，实测踩过）。
    window = [pool_ids[(start + offset) % pool_size] for offset in range(size)]
    started = time.perf_counter()

    items: list[dict[str, Any]] = []
    defect_count = 0
    for email_id in window:
        row = _cloud_effective(rows[email_id])
        # 云端回放的轨迹：triage/verifier 汇总真实归档结论，extractor 标注回放模式
        trace = [
            AgentStep(agent="triage", role=ROLE_TRIAGE,
                      summary=f"Archived category {row['category']} from the official pipeline run").as_dict(),
            AgentStep(agent="extractor", role="extractor",
                      summary="Cloud replay — archived extraction is authoritative (no attachment bytes on server)").as_dict(),
            AgentStep(agent="verifier", role=ROLE_VERIFIER,
                      summary=(f"Archived verdict {row['status']}"
                               + (f" on {', '.join(row['defect_fields'])}" if row["defect_fields"] else ""))).as_dict(),
            AgentStep(agent="escalation_judge", role=ROLE_JUDGE,
                      summary=(f"Manually reviewed ({row['manual_status']})" if row["manual_status"]
                               else (f"Escalated: {row['review_reason']}" if row["review_reason"]
                                     else "No escalation needed"))).as_dict(),
        ]
        defect_count += 1 if row["has_defect"] else 0
        items.append(StreamItem(
            email_id=email_id, subject=row["subject"], from_addr=row["from"],
            attachments=row["attachments"], category=row["category"], status=row["status"],
            has_defect=row["has_defect"], defect_fields=row["defect_fields"],
            review_reason=row["review_reason"],
            duration_ms=0, trace=trace).as_dict())

    return {
        "items": items, "cursor": CURSOR.position, "pool_size": len(pool_ids),
        "batch_size": len(items), "duration_ms": int((time.perf_counter() - started) * 1000),
        "defect_count": defect_count,
        "channel": "cloud-replay", "read_only": True,
        "source": "supabase",
    }


@app.get("/api/emails", summary="Audit queue with filtering and pagination")
def list_emails(
    status: str | None = Query(None, description="OK | MISMATCH | NEEDS_REVIEW"),
    category: str | None = Query(None, description="BL_COMPARISON | SI_REQUEST | …"),
    only_defect: bool = False,
    q: str | None = Query(None, description="Fuzzy search over email ID, subject or sender"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    dashboard = _local_dashboard()
    overrides = _load_overrides()
    needle = (q or "").strip().lower()
    items: list[dict[str, Any]] = []
    for email in dashboard["emails"]:
        record = _record_from_local(email, overrides)
        if status and record["status"] != status:
            continue
        if category and email["category"] != category:
            continue
        if only_defect and not record["has_defect"]:
            continue
        if needle and not any(
                needle in (email.get(key) or "").lower()
                for key in ("email_id", "subject", "from")):
            continue
        items.append({
            "email_id": email["email_id"], "subject": email["subject"],
            "from": email["from"], "category": record["category"],
            "status": record["status"], "has_defect": record["has_defect"],
            "defect_fields": record["defect_fields"],
            "review_reason": record["review_reason"],
            "attachments": email["attachments"],
            # 本地快照条目没有 overridden 键 → 回落旧口径（只看本地 overrides 文件）；
            # 云端行自带 manual_* 改判标记 → 与本地文件取或，徽章两种来源都认。
            "overridden": bool(email.get("overridden")) or email["email_id"] in overrides,
        })
    return {"total": len(items), "limit": limit, "offset": offset,
            "items": items[offset:offset + limit]}


@app.get("/api/emails/{email_id}", summary="Single email detail with the 7-field comparison")
def get_email(email_id: str) -> dict[str, Any]:
    dashboard = _local_dashboard()
    overrides = _load_overrides()
    for email in dashboard["emails"]:
        if email["email_id"] == email_id:
            return {"email": email, "submission": _record_from_local(email, overrides),
                    "override": overrides.get(email_id),
                    "field_labels": dashboard.get("field_labels", {})}
    if dashboard.get("source") == "supabase":
        detail = _cloud_email_detail(email_id)
        if detail is not None:
            return {"email": detail,
                    "submission": _record_from_local(detail, overrides),
                    "override": overrides.get(email_id),
                    "field_labels": dashboard.get("field_labels", {})}
    raise HTTPException(status_code=404, detail=f"No such email: {email_id}")


@app.get("/api/review-queue", summary="Human review queue, ordered by priority")
def review_queue(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    dashboard = _local_dashboard()
    overrides = _load_overrides()
    priority = {"missing_attachment": 0, "missing_value": 20,
                "unreadable": 30, "wrong_doc_type": 40}
    queue = [
        {"email_id": email["email_id"], "subject": email["subject"],
         "reason": email["review_reason"], "priority": priority.get(email["review_reason"] or "", 50),
         "handled": email["email_id"] in overrides}
        for email in dashboard["emails"] if email["status"] == "NEEDS_REVIEW"
    ]
    queue.sort(key=lambda item: (item["handled"], item["priority"], item["email_id"]))
    return {"total": len(queue), "items": queue[:limit]}


@app.post("/api/review", summary="Human-in-the-loop override (step 4 closes the loop)")
def review(payload: ReviewRequest) -> dict[str, Any]:
    if payload.status not in ("OK", "MISMATCH", "NEEDS_REVIEW"):
        raise HTTPException(status_code=422, detail=f"Invalid status: {payload.status}")
    invalid = sorted(set(payload.defect_fields) - set(COMPARE_FIELDS))
    if invalid:
        raise HTTPException(status_code=422,
                            detail=f"Invalid field names {invalid}; allowed: "
                                   f"{list(COMPARE_FIELDS)}")
    if payload.status == "MISMATCH" and not payload.defect_fields:
        raise HTTPException(status_code=422,
                            detail="MISMATCH requires at least one defect field")

    if _supabase_ready():
        from .db.repo import apply_manual_verdict
        try:
            result = apply_manual_verdict(
                payload.email_id, status=payload.status,
                defect_fields=payload.defect_fields, review_reason=payload.review_reason,
                note=payload.note, reviewer=payload.reviewer)
            return {"stored": "supabase", **result}
        except Exception as exc:      # noqa: BLE001 —— 云端失败则退回本地，界面不中断
            fallback_reason = f"{type(exc).__name__}: {exc}"
    else:
        fallback_reason = "Supabase 未配置"

    overrides = _load_overrides()
    overrides[payload.email_id] = {
        "status": payload.status,
        "defect_fields": sorted(set(payload.defect_fields)),
        "review_reason": payload.review_reason if payload.status == "NEEDS_REVIEW" else None,
        "note": payload.note, "reviewer": payload.reviewer,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    _save_overrides(overrides)
    return {"stored": "local-file", "path": str(LOCAL_OVERRIDES_PATH),
            "fallback_reason": fallback_reason,
            "email_id": payload.email_id, "status": payload.status}


@app.delete("/api/review/{email_id}", summary="Revert a human override")
def revert(email_id: str) -> dict[str, Any]:
    if _supabase_ready():
        from .db.repo import revert_manual_verdict
        try:
            return {"reverted": True, **revert_manual_verdict(email_id)}
        except Exception:      # noqa: BLE001
            pass
    overrides = _load_overrides()
    existed = overrides.pop(email_id, None)
    _save_overrides(overrides)
    return {"reverted": bool(existed), "stored": "local-file", "email_id": email_id}


@app.get("/api/submission", summary="Official 5-key submission payload (overrides applied)")
def submission() -> dict[str, Any]:
    result = current_submission()
    problems: list[str] = []
    for email_id, record in result.items():
        extra = set(record) - SUBMISSION_KEYS
        if extra:
            problems.append(f"{email_id}: extra keys {sorted(extra)}")
    return {"count": len(result), "problems": problems[:20], "submission": result}


@app.post("/api/submit", summary="Forward the payload to the official scoring service")
def submit_to_official(url: str = Query("http://localhost:8080")) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    payload = strip_diagnostics(current_submission())
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url.rstrip("/") + "/submit", data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return {"posted": len(payload), "url": url, "score": json.loads(response.read())}
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502,
                            detail=f"Official service returned HTTP {exc.code}: "
                                   f"{exc.read()[:300]!r}") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=503,
                            detail=f"Cannot reach the official scoring service at {url} "
                                   f"(start it with `docker compose up --build`): {exc.reason}") from exc


if __name__ == "__main__":      # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("API_PORT", "8000")),
                reload=os.environ.get("API_RELOAD", "1") == "1")
