"""FastAPI 契约测试 —— 用 TestClient 真打一遍 HTTP 路由。

盯三件事：
  1. 云端未配置时能优雅退回本地快照（演示/断网场景必须可用）；
  2. 人工改判真的会改变 /api/submission 的输出，且**不会**破坏官方 5 键口径；
  3. 非法输入（脏字段名、MISMATCH 空缺陷）被 422 拦下，不给前端破坏数据的机会。

    python -m tests.test_api
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from app.config import SUBMISSION_KEYS  # noqa: E402
from app.console import harden_console  # noqa: E402

# 测试期间把改判文件挪到临时路径，避免污染演示环境
TEST_OVERRIDES = ROOT / "eval" / "report" / "_test_manual_overrides.json"

import os  # noqa: E402

os.environ["MANUAL_OVERRIDES_PATH"] = str(TEST_OVERRIDES)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def _cleanup() -> None:
    TEST_OVERRIDES.unlink(missing_ok=True)


def test_health_reports_source_and_fields() -> None:
    _cleanup()
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["source"] in ("supabase", "local-snapshot")
    assert len(body["comparison_fields"]) == 7
    # 任何情况下都不得回显密钥内容，只允许 set/missing
    assert body["gemini_key"] in ("set", "missing")
    assert "service_role_key" in body["supabase"] or "sdk_installed" in body["supabase"]


def test_summary_and_emails_filters() -> None:
    _cleanup()
    summary = client.get("/api/summary")
    assert summary.status_code == 200
    stats = summary.json()["stats"]
    assert stats["total"] == 520, stats

    mismatches = client.get("/api/emails", params={"status": "MISMATCH", "limit": 500}).json()
    assert mismatches["total"] == 46, mismatches["total"]
    assert all(item["has_defect"] for item in mismatches["items"])
    assert all(item["category"] == "BL_COMPARISON" for item in mismatches["items"])

    spam = client.get("/api/emails", params={"category": "SPAM", "limit": 500}).json()
    assert spam["total"] == 40
    assert all(item["status"] == "OK" for item in spam["items"])


def test_email_detail_has_seven_fields() -> None:
    _cleanup()
    body = client.get("/api/emails/email_004").json()
    email = body["email"]
    assert email["email_id"] == "email_004"
    assert len(email["fields"]) == 7
    assert set(body["submission"]) == SUBMISSION_KEYS
    assert body["submission"]["defect_fields"] == ["consignee", "notify_party"]

    missing = client.get("/api/emails/email_999999")
    assert missing.status_code == 404


def test_review_queue_only_needs_review() -> None:
    _cleanup()
    queue = client.get("/api/review-queue", params={"limit": 500}).json()
    assert queue["total"] >= 20
    assert all(item["reason"] for item in queue["items"])


def test_manual_verdict_flows_into_submission() -> None:
    _cleanup()
    before = client.get("/api/submission").json()
    assert before["count"] == 520
    assert not before["problems"], before["problems"][:3]

    # 把一封 MISMATCH 改判为一致
    response = client.post("/api/review", json={
        "email_id": "email_004", "status": "OK", "note": "人工复核：BL 已更新",
        "reviewer": "tester",
    })
    assert response.status_code == 200, response.text

    after = client.get("/api/submission").json()["submission"]
    assert after["email_004"]["status"] == "OK"
    assert after["email_004"]["has_defect"] is False
    assert after["email_004"]["defect_fields"] == []

    # 再改判成一封带缺陷的，字段集合必须与输入完全一致（官方按集合相等计分）
    response = client.post("/api/review", json={
        "email_id": "email_004", "status": "MISMATCH",
        "defect_fields": ["gross_weight_kg"], "note": "只认毛重差异", "reviewer": "tester",
    })
    assert response.status_code == 200, response.text
    after = client.get("/api/submission").json()["submission"]
    assert after["email_004"]["defect_fields"] == ["gross_weight_kg"]
    assert after["email_004"]["has_defect"] is True

    # 撤销后回到机器裁决
    assert client.delete("/api/review/email_004").json()["reverted"] is True
    restored = client.get("/api/submission").json()["submission"]
    assert restored["email_004"]["defect_fields"] == ["consignee", "notify_party"]
    _cleanup()


def test_non_comparison_email_is_always_clean_ok() -> None:
    """SPAM/GENERAL 被改判也必须归一成干净 OK —— 与 SQL 视图口径一致。"""
    _cleanup()
    response = client.post("/api/review", json={
        "email_id": "email_015", "status": "MISMATCH",
        "defect_fields": ["shipper"], "reviewer": "tester",
    })
    assert response.status_code == 200, response.text
    record = client.get("/api/submission").json()["submission"]["email_015"]
    assert record == {"category": "SPAM", "status": "OK", "review_reason": None,
                      "has_defect": False, "defect_fields": []}, record
    _cleanup()


def test_invalid_inputs_are_rejected() -> None:
    _cleanup()
    dirty_field = client.post("/api/review", json={
        "email_id": "email_004", "status": "MISMATCH",
        "defect_fields": ["not_a_field"], "reviewer": "tester",
    })
    assert dirty_field.status_code == 422
    assert "Invalid field names" in dirty_field.json()["detail"]

    empty_defect = client.post("/api/review", json={
        "email_id": "email_004", "status": "MISMATCH", "defect_fields": [],
    })
    assert empty_defect.status_code == 422

    bad_status = client.post("/api/review", json={
        "email_id": "email_004", "status": "MAYBE", "defect_fields": [],
    })
    assert bad_status.status_code == 422
    assert not TEST_OVERRIDES.exists()


def test_submit_proxies_and_reports_unreachable_official_service() -> None:
    """官方服务没起时必须 503 + 可执行的提示，而不是 500 栈。"""
    _cleanup()
    response = client.post("/api/submit", params={"url": "http://127.0.0.1:9"})
    assert response.status_code == 503
    assert "docker compose" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 「上传即核对」——核心交互契约
# ---------------------------------------------------------------------------
# 内联合成两份单据（不依赖 data/ 已解包，干净克隆也能跑），
# 刻意在 container_count 上制造 1 vs 3 的不一致，形如官方语料 email_031。
_UPLOAD_SI = """SHIPPING INSTRUCTION
========================================
Shipper: APRIL FINE PAPER TRADING (MIDDLE EAST) FZE
  #813, 4 EA, DUBAI AIRPORT FREE ZONE; P.O. BOX: 293775, DUBAI, UNITED ARAB EMIRATES
Consignee (Non-Negotiable): VITAL SOLUTIONS PTE. LTD.
  77 ROBINSON ROAD; #21-01 ROBINSON 77; SINGAPORE 068896
NOTIFY PARTY: VITAL SOLUTIONS PTE. LTD.
PORT OF LOADING: NHAVA SHEVA, INDIA (INNSA)
Port of Discharge: MOMBASA, KENYA (KEMBA)
No. of Containers: 1 x 20'GP
Gross Wt (kgs): 21,114 KG
"""

_UPLOAD_BL = """BILL OF LADING (DRAFT)
========================================
Shipper (Principal or Seller): APRIL FINE PAPER TRADING (MIDDLE EAST) FZE
  #813, 4 EA, DUBAI AIRPORT FREE ZONE; P.O. BOX: 293775, DUBAI, UNITED ARAB EMIRATES
CONSIGNEE: VITAL SOLUTIONS PTE. LTD.
  77 ROBINSON ROAD; #21-01 ROBINSON 77; SINGAPORE 068896
Notify Party: VITAL SOLUTIONS PTE. LTD.
Port of Loading: NHAVA SHEVA, INDIA (INNSA)
POD: MOMBASA, KENYA (KEMBA)
Container Count: 3 x 20'GP
Gross Wt (kgs): 23,114 KG
"""


def _post_upload(**kwargs) -> object:
    """统一入口：**关闭 LLM 与云写入**，保证测试确定、离线、不花钱。"""
    form = {"use_llm": "false", "persist_cloud": "false", **kwargs.pop("data", {})}
    files = kwargs.pop("files")
    return client.post("/api/verify", files=files, data=form)


def test_upload_verify_returns_seven_field_rows() -> None:
    """上传一对 SI/BL → 返回与工作台快照同构的 7 字段逐项判定。"""
    response = _post_upload(files=[
        ("files", ("probe_SI.txt", _UPLOAD_SI, "text/plain")),
        ("files", ("probe_BL.txt", _UPLOAD_BL, "text/plain")),
    ])
    assert response.status_code == 200, response.text
    payload = response.json()
    record = payload["record"]
    assert len(record["fields"]) == 7
    assert record["category"] == "BL_COMPARISON"
    # 1 vs 3 个箱子 + 毛重也不一致 → 必须判 MISMATCH 且只列出真实差异
    assert record["status"] == "MISMATCH"
    assert record["defect_fields"] == ["container_count", "gross_weight_kg"]
    assert record["has_defect"] is True
    # 前端直接靠 verdict 画红框
    verdicts = {row["field"]: row["verdict"] for row in record["fields"]}
    assert verdicts["container_count"] == "defect"
    assert verdicts["shipper"] == "match"
    assert payload["ai"]["used"] is False      # 本次刻意走规则通道


def test_upload_verify_single_side_escalates_with_reason() -> None:
    """只上传一份 → 走升级阶梯给 NEEDS_REVIEW + 理由，而不是报错。"""
    response = _post_upload(files=[("files", ("probe_SI.txt", _UPLOAD_SI, "text/plain"))])
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert record["status"] == "NEEDS_REVIEW"
    assert record["review_reason"] == "missing_attachment"
    assert record["defect_fields"] == []


def test_upload_verify_rejects_bad_input() -> None:
    """空文件 / 不支持的扩展名 → 422（可读信息，不是 500 栈）。"""
    empty = _post_upload(files=[("files", ("probe_SI.txt", b"", "text/plain"))])
    assert empty.status_code == 422
    assert "empty file" in empty.json()["detail"]

    bad_type = _post_upload(files=[("files", ("probe.exe", b"MZ", "application/octet-stream"))])
    assert bad_type.status_code == 422
    assert "Unsupported file type" in bad_type.json()["detail"]


def test_upload_never_pollutes_official_submission() -> None:
    """★★ 红线测试：上传绝不能改变官方 520 键提交产物。

    为什么必须钉死：submission_view 是从 emails 表聚合的，而官方对
    missing/extra 键都会扣分。若上传件误入 emails，现场演示只要点一次上传，
    已拿到的满分就当场作废 —— 这条测试就是那道闸门。
    """
    _cleanup()
    before = client.get("/api/submission").json()
    response = _post_upload(files=[
        ("files", ("probe_SI.txt", _UPLOAD_SI, "text/plain")),
        ("files", ("probe_BL.txt", _UPLOAD_BL, "text/plain")),
    ])
    assert response.status_code == 200, response.text
    after = client.get("/api/submission").json()
    assert after["count"] == before["count"]
    assert set(after["submission"]) == set(before["submission"])
    assert not any(key.startswith("upload_") for key in after["submission"])


def test_summary_exposes_roi_cards() -> None:
    """GBS ROI 三张大卡的 API 口径：数值必须来自真实跑批产物。"""
    _cleanup()
    body = client.get("/api/summary").json()
    roi = body.get("roi")
    assert roi, "本地快照必须带 roi"
    assert roi["emails_total"] == 520
    assert roi["defects_caught"] == 46, roi["defects_caught"]
    # 人工 20min/对 × 126 对 = 42h；机器耗时必须是实测的正数
    assert roi["human_seconds"] == 126 * 20 * 60
    assert 0 < roi["machine_seconds"] < 60
    assert roi["time_saved_ratio"] > 0.99
    assert roi["token_cost_saved_usd"] > 0 and roi["annual_total_saved_usd"] > 0
    # 假设必须显式暴露，评委能改 env 重算
    assert roi["assumptions"]["human_minutes_per_pair"] == 20.0
    assert "machine_time_source" in roi["provenance"]


def test_agents_roster_and_live_trace() -> None:
    """多 Agent：花名册 + 现算轨迹，且轨迹结论必须与提交产物一致。"""
    _cleanup()
    roster = client.get("/api/agents").json()
    # agent 用机器 id，role 是两份来源（导出快照 / 现算轨迹）共同的连接键
    assert [item["agent"] for item in roster["agents"]] == [
        "triage", "extractor", "cross_verifier", "escalation_judge"]
    assert [item["role"] for item in roster["agents"]] == \
        ["triage", "extractor", "verifier", "judge"]
    assert roster["pipeline"] == ["Triage", "Extract", "CrossVerify", "Escalate"]

    summary = client.get("/api/emails/email_031").json()["email"]
    assert [step["agent"] for step in summary["trace"]] == \
        [item["agent"] for item in roster["agents"]], "快照轨迹的 agent id 必须与花名册一致"

    trace = client.get("/api/agents/trace/email_031").json()
    assert trace["email_id"] == "email_031"
    assert trace["llm_used"] is False          # 测试绝不发 API 调用
    steps = trace["trace"]["steps"]
    assert [step["role"] for step in steps] == ["triage", "extractor", "verifier", "judge"]
    assert all("summary" in step for step in steps)
    assert trace["record"]["status"] == "MISMATCH"
    assert trace["record"]["defect_fields"] == ["container_count", "gross_weight_kg"]
    # ★ 现场自证：我展示的轨迹和拿去打分的产物是同一个结论
    assert trace["matches_submission"] is True

    missing = client.get("/api/agents/trace/email_999999")
    assert missing.status_code == 404


def test_stream_pull_rotates_and_never_touches_submission() -> None:
    """★ 实时拉取：只读、环状轮转、结论可用，且绝不污染 520 键提交集。"""
    _cleanup()
    before = client.get("/api/submission").json()["submission"]

    first = client.post("/api/stream/pull", params={"batch_size": 4, "reset": True}).json()
    assert first["batch_size"] == 4
    assert first["read_only"] is True
    assert first["channel"] == "deterministic"
    for item in first["items"]:
        assert item["status"] in ("OK", "MISMATCH", "NEEDS_REVIEW")
        assert len(item["trace"]) == 4, item["email_id"]
        assert [step["role"] for step in item["trace"]] == ["triage", "extractor", "verifier", "judge"]
        assert all(step["duration_ms"] >= 0 for step in item["trace"])
        assert item["defect_fields"] == sorted(item["defect_fields"])

    second = client.post("/api/stream/pull", params={"batch_size": 4}).json()
    assert {item["email_id"] for item in first["items"]} & \
        {item["email_id"] for item in second["items"]} == set(), "游标必须前进，不能重复拉同一批"

    after = client.get("/api/submission").json()["submission"]
    assert set(after) == set(before)
    assert all(after[key] == before[key] for key in before)


def test_ai_status_exposes_resolved_models_without_calling_api() -> None:
    """AI 状态端点：只读缓存，形状稳定（不因无 Key 而 500）。"""
    response = client.get("/api/ai-status")
    assert response.status_code == 200
    body = response.json()
    assert "key_configured" in body and "sdk_available" in body
    if body["key_configured"]:
        assert "classify" in body["candidate_chains"]
        assert "resolved" in body


def main() -> int:
    harden_console()
    _cleanup()
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failures: list[str] = []
    for name, test in tests:
        try:
            test()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failures.append(name)
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    _cleanup()
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
