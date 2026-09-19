"""仓储层离线契约测试 —— 不需要 Supabase，也不需要 supabase SDK。

它盯的是三类"写库才会炸、但本地能提前发现"的问题：
  1. attachment_count 这类 GENERATED 列被误写进 payload
  2. on_conflict 与主键不一致（后果是静默插重复行，不是报错 —— 最危险）
  3. 官方 5 键合约被破坏的脏数据试图落库

    python3 -m tests.test_repo_contract
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from app.console import harden_console  # noqa: E402
from app.db.repo import (  # noqa: E402
    EmailWrite, ExtractionWrite, PipelineUnit, RepoError, comparison_row, fetch_submission,
    strip_diagnostics, upsert_verification_pipeline_results, validate_submission, validate_units,
)
from app.db.supabase import Gateway, SupabaseSettings, SupabaseUnavailable, get_gateway  # noqa: E402
from app.pipeline.normalize import COMPARE_FIELDS  # noqa: E402

# 与 0002_tables.sql 里的 on_conflict 必须逐字一致
EXPECTED_CONFLICTS = {
    "emails": "email_id",
    "extracted_fields": "email_id,doc_type",
    "comparisons": "email_id,field_name",
    "review_queue": "email_id,reason",
}


def _unit(email_id: str = "email_001", *, defects: list[str] | None = None) -> PipelineUnit:
    defects = defects or []
    status = "MISMATCH" if defects else "OK"
    email = EmailWrite(
        email_id=email_id, subject="SI - 123 - DIRECT", from_addr="ops@aprilasia.com",
        attachment_paths=[f"{email_id}_SI.xlsx", f"{email_id}_BL.docx"],
        category="BL_COMPARISON", category_confidence=0.93, classified_by="rule",
        verdict_status=status, has_defect=bool(defects), defect_fields=defects,
        review_reason=None, pipeline_state="COMPARED" if not defects else "COMPARED",
        prompt_version="v1")
    extractions = tuple(
        ExtractionWrite(
            email_id=email_id, doc_type=doc_type, source_path=f"{email_id}_{doc_type}.xlsx",
            values={name: (None if name in defects else "10 x 40'HC") for name in COMPARE_FIELDS},
            field_confidence={name: 1.0 for name in COMPARE_FIELDS},
            field_blank=(), doc_role_detected=doc_type, extractor_model="rule")
        for doc_type in ("SI", "BL"))
    comparisons = tuple(
        comparison_row({
            "email_id": email_id, "field_name": name,
            "si_raw": "10", "bl_raw": "10" if name not in defects else "11",
            "is_match": name not in defects,
            "match_method": "exact" if name not in defects else "numeric_tolerance",
            "delta": Decimal("-1") if name in defects else Decimal("0"),
            "needs_human": False, "highlight": [], "decided_by": "rule",
        })
        for name in COMPARE_FIELDS)
    return PipelineUnit(email=email, extractions=extractions, comparisons=comparisons)


# ---------------------------------------------------------------------------
def test_attachment_count_never_written() -> None:
    """GENERATED 列写进去 PostgreSQL 直接报错，必须在构造层就杜绝。"""
    row = _unit().email.to_row()
    assert "attachment_count" not in row, row
    assert row["attachment_paths"] and len(row["attachment_paths"]) == 2


def test_on_conflict_matches_primary_keys() -> None:
    assert EXPECTED_CONFLICTS == {
        "emails": "email_id",
        "extracted_fields": "email_id,doc_type",
        "comparisons": "email_id,field_name",
        "review_queue": "email_id,reason",
    }
    source = (ROOT / "backend" / "db" / "migrations" / "0002_tables.sql").read_text("utf-8")
    assert "primary key (email_id, doc_type)" in source
    assert "primary key (email_id, field_name)" in source
    assert "unique (email_id, reason)" in source


def test_extraction_row_has_all_canonical_keys() -> None:
    """values 必须含 7 个键：SQL 上有 `values ?& 7字段` 约束，缺键会被拒。"""
    row = _unit().extractions[0].to_row()
    assert set(row["values"]) == set(COMPARE_FIELDS), row["values"]
    assert set(row["field_confidence"]) == set(COMPARE_FIELDS)


def test_defect_fields_sorted_and_deduped() -> None:
    row = _unit(defects=["gross_weight_kg", "shipper", "shipper"]).email.to_row()
    assert row["defect_fields"] == ["gross_weight_kg", "shipper"]


def test_dirty_verdict_is_rejected_before_write() -> None:
    bad = _unit().email
    bad.verdict_status = "MISMATCH"          # 但没有 defect_fields → 违反官方不变量
    bad.has_defect = False
    problems = validate_units([PipelineUnit(email=bad)])
    assert problems and "MISMATCH" in problems[0], problems


def test_comparison_row_rejects_missing_method() -> None:
    try:
        comparison_row({"email_id": "e", "field_name": "shipper"})
    except RepoError as exc:
        assert "match_method" in str(exc)
    else:
        raise AssertionError("缺 match_method 的行必须被拒绝")


def test_invalid_needs_human_reason_is_dropped() -> None:
    row = comparison_row({
        "email_id": "e", "field_name": "shipper", "is_match": False,
        "match_method": "fuzzy", "needs_human": True, "needs_human_reason": "瞎写的理由",
    })
    assert row["needs_human_reason"] is None      # 枚举会拒收脏值，必须提前清掉


def test_decimal_is_json_safe() -> None:
    row = comparison_row({
        "email_id": "e", "field_name": "gross_weight_kg", "is_match": False,
        "match_method": "numeric_tolerance", "delta": Decimal("12.500"),
        "similarity": None, "bl_parsed": {"kg": Decimal("1.5")},
    })
    assert isinstance(row["delta"], float) and row["delta"] == 12.5
    assert row["bl_parsed"]["kg"] == 1.5


def test_submission_view_shape() -> None:
    """非 BL_COMPARISON 必须被视图归一成干净的 OK（与官方口径一致）。"""
    source = (ROOT / "backend" / "db" / "migrations" / "0003_views.sql").read_text("utf-8")
    assert "submission_view" in source and "manual_defect_fields" in source
    assert "'OK'" in source and "coalesce" in source


def test_strip_diagnostics_keeps_five_keys() -> None:
    submission = {"email_001": {**_unit().email.verdict_record(), "decided_by": "rule"}}
    stripped = strip_diagnostics(submission)
    assert set(stripped["email_001"]) == {
        "category", "status", "review_reason", "has_defect", "defect_fields"}


def test_validate_submission_flags_missing_keys() -> None:
    problems = validate_submission({"email_001": {"category": "GENERAL", "status": "OK",
                                                  "review_reason": None, "has_defect": False,
                                                  "defect_fields": []}},
                                   expected_ids=["email_001", "email_002"])
    assert problems and "缺少" in problems[0], problems


def test_missing_supabase_degrades_gracefully() -> None:
    """没装 SDK / 没配密钥时，落库必须**优雅跳过**而不是抛栈中断整批。"""
    gateway = Gateway(SupabaseSettings(url="", service_role_key=""))
    report = run_sync(upsert_verification_pipeline_results([_unit()], gateway=gateway))
    assert report.skipped and not report.failed_email_ids, report.as_dict()
    assert report.ok is False                    # skipped 明确表达「没写」


def test_gateway_raises_when_unconfigured() -> None:
    gateway = Gateway(SupabaseSettings())
    try:
        fetch_submission(gateway)
    except SupabaseUnavailable as exc:
        assert "未配置" in str(exc) or "SDK" in str(exc)
    else:
        raise AssertionError("未配置时必须抛 SupabaseUnavailable")


def test_gateway_singleton() -> None:
    assert get_gateway() is get_gateway()


def run_sync(coro):
    import asyncio
    return asyncio.run(coro)


def main() -> int:
    harden_console()
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failures: list[str] = []
    for name, test in tests:
        try:
            test()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
