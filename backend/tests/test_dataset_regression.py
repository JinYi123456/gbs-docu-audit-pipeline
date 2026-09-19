"""端到端数据集回归 —— 全链路跑 520 封，用**官方** scoring.py 打分。

跑的是纯确定性路径（规则分类 → 读附件 → 规则抽取 → 硬归一 → 判定矩阵 → 升级阶梯），
不消耗任何 API 额度、不需要网络。它的作用是：
  1. 锁死阈值：任何人改动 normalize/compare 的规则，命中率下降立刻红
  2. 给出真实分值基线，让 LLM 通道的增益可量化
  3. 在没有 API Key 的机器上也能验证全链路连通

    python3 -m tests.test_dataset_regression            # 全量 520 封
    python3 -m tests.test_dataset_regression --limit 50 # 抽样快跑
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from app.config import DEFAULT_SUBMISSION_PATH, resolve_data_dir  # noqa: E402
from app.console import harden_console  # noqa: E402
from app.eval_support import (  # noqa: E402
    load_ground_truth, print_confusion, print_field_diagnosis, print_scoreboard,
    score_offline, snapshot, strip_diagnostics,
)
from app.ingest.inbox import InboxSource  # noqa: E402
from app.pipeline.classify import CATEGORY_BL_COMPARISON  # noqa: E402
from app.runner import EMPTY_RECORD, process_email_deterministic  # noqa: E402

# 确定性路径的验收线（低于此值说明归一层/判定矩阵被改坏了）
MIN_STAGE1_MACRO_F1 = 0.90
MIN_E2E_RATE = 0.80
MIN_DEFECT_F1 = 0.90


def run_deterministic(
    *,
    limit: int | None = None,
    data_dir: str = "data",
) -> tuple[dict[str, dict], dict[str, int]]:
    source = InboxSource("data", data_dir=resolve_data_dir(data_dir))
    emails = source.emails()
    if limit:
        emails = emails[:limit]

    submission: dict[str, dict] = {}
    stats: Counter = Counter()
    for email in emails:
        try:
            record, classification = process_email_deterministic(source, email)
        except Exception as exc:  # noqa: BLE001
            record = dict(EMPTY_RECORD)
            classification = None
            stats[f"error:{type(exc).__name__}"] += 1
        submission[email.email_id] = {
            **record,
            # decided_by 会让官方 score_stage1 算出 rule_pct（诊断项，不计分）
            "decided_by": (classification.decided_by if classification is not None else "rule"),
        }
        if classification is not None:
            stats[f"category:{classification.category}"] += 1
            stats[f"status:{record['status']}"] += 1
    return submission, dict(stats)


def main(argv: list[str] | None = None) -> int:
    harden_console()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--out", type=Path, default=DEFAULT_SUBMISSION_PATH)
    parser.add_argument("--no-score", action="store_true", help="只跑链路，不打分")
    parser.add_argument("--no-snapshot", action="store_true")
    parser.add_argument("--with-diagnostics", action="store_true",
                        help="保留 decided_by（官方据此算 rule_pct，慎用于正式提交）")
    parser.add_argument("--dashboard", type=Path, default=None,
                        help="额外导出人审工作台数据（JSON，供前端读取）")
    parser.add_argument("--tag", default="deterministic")
    args = parser.parse_args(argv)

    print("=" * 80)
    print("确定性全链路回归（规则分类 + 规则抽取 + 判定矩阵 + 升级阶梯）")
    print("=" * 80)
    submission, stats = run_deterministic(limit=args.limit, data_dir=args.data_dir)

    def _sum_by_prefix(prefix: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for key, value in stats.items():
            if key.startswith(prefix):
                name = key.split(":", 1)[1]
                out[name] = out.get(name, 0) + int(value)
        return dict(sorted(out.items(), key=lambda item: -item[1]))

    counts = _sum_by_prefix("category:")
    statuses = _sum_by_prefix("status:")
    errors = {key: value for key, value in stats.items() if key.startswith("error:")}
    print(f"\n邮件数：{len(submission)}")
    print(f"分类分布：{counts}")
    print(f"裁决分布：{statuses}")
    if errors:
        print(f"异常：{errors}")

    # 默认剥掉 decided_by：提交产物严格 5 键；诊断模式下保留以便官方算出 rule_pct
    submission = submission if args.with_diagnostics else strip_diagnostics(submission)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(dict(sorted(submission.items())), indent=2,
                                   ensure_ascii=False), encoding="utf-8")
    print(f"产物：{args.out}")

    if args.dashboard:
        from app.dashboard import build_dashboard, write_dashboard
        payload = build_dashboard(submission, data_dir=args.data_dir)
        print(f"看板数据：{write_dashboard(payload, args.dashboard)}")

    if args.no_score:
        return 0

    try:
        truth = load_ground_truth()
        if args.limit:
            # 抽样跑批时只在子集上打分：否则缺失的键会被官方当成 GENERAL 记罚，
            # 打出来的分会低到毫无参考价值（这个坑我踩过一次）。
            truth = {email_id: gold for email_id, gold in truth.items()
                     if email_id in submission}
            board = score_offline(submission, ground_truth_override=truth)
            print(f"\n[提示] 抽样模式：仅在 {len(truth)} 封子集上打分")
        else:
            board = score_offline(submission)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[skip] 无法打分：{type(exc).__name__}: {exc}")
        return 0

    print_scoreboard(board, label="(deterministic)")
    print("\n错分与字段诊断：")
    print_confusion(board)
    print_field_diagnosis(truth, submission)

    reference = {email_id: {"category": record["category"], "status": "OK",
                            "review_reason": None, "has_defect": False,
                            "defect_fields": []}
                 for email_id, record in submission.items()}
    reference_board = score_offline(reference, ground_truth_override=truth)
    print("\n  [参考] 若 status 一律填 OK（仅分类无比对）："
          f"final={reference_board['final_score']:.4f}")

    if not args.no_snapshot:
        snapshot(board, submission, tag=args.tag)

    stage1 = board["stage1"]["macro_f1"]
    e2e = board["end_to_end"]["rate"]
    defect_f1 = board["stage3"]["defect_f1"]
    ok = (stage1 >= MIN_STAGE1_MACRO_F1 and e2e >= MIN_E2E_RATE
          and defect_f1 >= MIN_DEFECT_F1)
    print(f"\n验收线：macro_f1>={MIN_STAGE1_MACRO_F1} e2e>={MIN_E2E_RATE} "
          f"defect_f1>={MIN_DEFECT_F1} → {'通过' if ok else '未达标'}")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# pytest 入口
# ---------------------------------------------------------------------------
def _board_for_full_run() -> dict:
    submission, _ = run_deterministic()
    return score_offline(submission)


def test_stage1_macro_f1_above_floor() -> None:
    board = _board_for_full_run()
    assert board["stage1"]["macro_f1"] >= MIN_STAGE1_MACRO_F1, board["stage1"]


def test_defect_f1_above_floor() -> None:
    board = _board_for_full_run()
    assert board["stage3"]["defect_f1"] >= MIN_DEFECT_F1, board["stage3"]


def test_end_to_end_above_floor() -> None:
    board = _board_for_full_run()
    assert board["end_to_end"]["rate"] >= MIN_E2E_RATE, board["end_to_end"]


def test_no_escalation_when_defect_found() -> None:
    """铁律回归：MISMATCH 绝不允许带 review_reason 或 has_defect=false。"""
    submission, _ = run_deterministic()
    bad = [email_id for email_id, record in submission.items()
           if record["status"] == "MISMATCH"
           and (record["review_reason"] or not record["has_defect"])]
    assert not bad, f"违反铁律：{bad[:5]}"


def test_bl_comparison_pairs_get_compared() -> None:
    """带 SI+BL 两个附件的邮件必须真正走到比对，不能整批落在 GENERAL/OK。"""
    submission, _ = run_deterministic()
    truth = load_ground_truth()
    comparable = [email_id for email_id, gold in truth.items()
                  if gold["category"] == "BL_COMPARISON" and gold["status"] != "NEEDS_REVIEW"]
    mismatches = [email_id for email_id in comparable
                  if submission.get(email_id, {}).get("has_defect")]
    assert len(mismatches) >= 40, f"只检出 {len(mismatches)} 封缺陷，期望 >=40"


if __name__ == "__main__":
    sys.exit(main())
