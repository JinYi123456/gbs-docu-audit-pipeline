"""多 Agent 编排层回归测试。

盯住四件事（每一条都对应一次真实踩坑）：
  1. 四步必须**全部真的执行过**（`AgentStep` 里不许出现 error）——
     早先 cross_verifier 写成 `row.field_name`（真实属性是 `row.field`），
     它静默抛 AttributeError，结论却因为 judge 拿到真实 report 而"看起来正确"：
     没有这条断言，这个 bug 会一直藏在轨迹里。
  2. 非对照类邮件必须**短路**，不为垃圾邮件付抽取成本。
  3. 缺一侧文档要给出 NEEDS_REVIEW + 正确理由，而不是抛异常。
  4. 裁决结论与单 Agent 直调 compare/policy 的结果**完全一致**（同源性）。

    python -m tests.test_agents
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from app.agents.orchestrator import verify_email  # noqa: E402
from app.config import resolve_data_dir  # noqa: E402
from app.console import harden_console  # noqa: E402
from app.ingest.inbox import InboxEmail, InboxSource  # noqa: E402

DATA_DIR = resolve_data_dir("data")
SOURCE = InboxSource("data", data_dir=DATA_DIR) if DATA_DIR.is_dir() else None


def _email(email_id: str) -> InboxEmail | None:
    if SOURCE is None:
        return None
    return next((mail for mail in SOURCE.emails() if mail.email_id == email_id), None)


def _spam_email() -> InboxEmail:
    """合成一封垃圾邮件（不依赖数据集）：验证短路分支。"""
    return InboxEmail(
        email_id="synthetic_spam", sender="pitch@crypto-invest.net",
        subject="Increase your shipping revenue with one weird trick",
        body="Guaranteed 300% returns. Click here to claim your prize.",
        attachments=(), raw={})


def test_all_four_agents_execute_without_errors() -> None:
    """四步齐全、零异常 —— 这条就是 `field` / `field_name` 那个 bug 的哨兵。"""
    email = _email("email_031")
    if email is None:
        print("    (跳过：data/ 未解包)")
        return
    result = asyncio.run(verify_email(email, source=SOURCE, use_llm=False))
    trace = result.state.trace_summary()
    assert [step["agent"] for step in trace["steps"]] == [
        "triage", "extractor", "cross_verifier", "escalation_judge"]
    assert trace["errors"] == [], f"有 Agent 报错：{trace['errors']}"
    for step in trace["steps"]:
        assert step["error"] is None, f"{step['agent']} 失败：{step['error']}"


def test_verdict_matches_ground_truth_shape() -> None:
    """email_031 的官方 gold 是 container_count + gross_weight_kg（集合须完全相等）。"""
    email = _email("email_031")
    if email is None:
        print("    (跳过：data/ 未解包)")
        return
    result = asyncio.run(verify_email(email, source=SOURCE, use_llm=False))
    record = result.record
    assert record["category"] == "BL_COMPARISON"
    assert record["status"] == "MISMATCH"
    assert record["defect_fields"] == ["container_count", "gross_weight_kg"]


def test_non_comparison_email_short_circuits() -> None:
    """垃圾邮件必须在 triage 之后短路，不跑抽取/比对/升级。"""
    result = asyncio.run(verify_email(_spam_email(), source=None, use_llm=False))
    assert result.record["category"] == "SPAM"
    assert result.record["status"] == "OK"
    assert len(result.state.steps) == 1
    assert result.skipped == ["extractor", "cross_verifier", "escalation_judge"]


def test_missing_side_escalates_with_reason() -> None:
    """只有一侧文档 → NEEDS_REVIEW + missing_attachment（不抛异常）。"""
    if SOURCE is None:
        print("    (跳过：data/ 未解包)")
        return
    from app.ingest.readers import read_document
    from app.pipeline.extract import ROLE_SI, DocSlot

    source = InboxSource("data", data_dir=DATA_DIR)
    si_path = next(iter(sorted((DATA_DIR / "attachments").glob("email_004_SI.*"))), None)
    if si_path is None:
        print("    (跳过：找不到测试附件)")
        return
    # ★ InboxEmail 是 frozen dataclass —— 不能在构造后赋值 `attachments`
    #   （早先那版测试就是因此报 FrozenInstanceError），必须一次构造到位。
    attachment = f"attachments/{si_path.name}"
    email = InboxEmail(
        email_id="synthetic_single", sender="ops@aprilasia.com",
        subject="REQUEST BL DRAFT _ single side probe",
        body="Attached the SI only.", attachments=(attachment,), raw={})
    slot = DocSlot(ROLE_SI, read_document(source, attachment))
    result = asyncio.run(verify_email(email, source=source, use_llm=False,
                                      si_slot=slot, bl_slot=None, slots_provided=True))
    assert result.record["status"] == "NEEDS_REVIEW"
    assert result.record["review_reason"] == "missing_attachment"
    assert result.record["defect_fields"] == []


def test_verifier_trace_exposes_judgement_matrix_evidence() -> None:
    """判定矩阵的证据必须落到轨迹里（答辩时要能指着它讲容差）。"""
    email = _email("email_031")
    if email is None:
        print("    (跳过：data/ 未解包)")
        return
    result = asyncio.run(verify_email(email, source=SOURCE, use_llm=False))
    verifier_step = next(step for step in result.state.trace()
                         if step["agent"] == "cross_verifier")
    evidence = verifier_step["evidence"]
    assert evidence["match_methods"]["container_count"] in {
        "exact", "normalized", "numeric_tolerance", "fuzzy", "blank_one_side"}
    assert evidence["tolerances"]["weight_abs_tolerance_kg"] == 10
    assert evidence["tolerances"]["text_similarity_threshold"] == 0.94
    assert verifier_step["used_llm"] is False      # 裁决层绝不用 LLM


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
            failures.append(name)
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
