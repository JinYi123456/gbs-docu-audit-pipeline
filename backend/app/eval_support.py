"""自评测支撑：直接复用**官方** server/scoring.py，绝不重写打分逻辑。

口径必须与组织方一致，否则我们会对着一个错误的分数优化 —— 这是本项目里
最不值得犯的错误。所以这里只做两件事：装载官方模块、把结果画成人看的表。
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import (
    DEFAULT_GROUND_TRUTH, DEFAULT_REPORT_DIR, DEFAULT_SCORING_MODULE, SUBMISSION_KEYS,
)

BEST_FILE_NAME = "best.json"


class ScoringUnavailable(RuntimeError):
    pass


def _load_official_scoring(scoring_path: Path | None = None) -> Any:
    path = Path(scoring_path or DEFAULT_SCORING_MODULE)
    if not path.is_file():
        raise ScoringUnavailable(
            f"找不到官方打分器 {path}。跑 `make bootstrap`（或解压 docker 包）后重试。")
    spec = importlib.util.spec_from_file_location("sdoc_official_scoring", path)
    if spec is None or spec.loader is None:
        raise ScoringUnavailable(f"无法加载官方打分器 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_ground_truth(ground_truth_path: Path | None = None) -> dict[str, Any]:
    path = Path(ground_truth_path or DEFAULT_GROUND_TRUTH)
    if not path.is_file():
        raise ScoringUnavailable(
            f"找不到答案键 {path}（仅用于本地开发回归，.gitignore 已排除，绝不入库）")
    return json.loads(path.read_text(encoding="utf-8"))


def score_offline(
    submission: Mapping[str, Any],
    *,
    ground_truth_path: Path | None = None,
    scoring_path: Path | None = None,
    ground_truth_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """用官方 scoring.py 离线打分。

    ground_truth_override 用于抽样跑批：只在子集上打分，避免缺失键被当成 GENERAL 记罚。
    """
    module = _load_official_scoring(scoring_path)
    truth = dict(ground_truth_override) if ground_truth_override is not None \
        else load_ground_truth(ground_truth_path)
    return module.score_all(truth, dict(submission))


def strip_diagnostics(submission: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """官方提交只允许 5 个键；decided_by 仅在诊断模式下保留。"""
    return {email_id: {key: value for key, value in record.items() if key in SUBMISSION_KEYS}
            for email_id, record in submission.items()}


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def _bar(value: float, width: int = 22) -> str:
    filled = int(round(max(0.0, min(1.0, value)) * width))
    return "#" * filled + "." * (width - filled)


def print_scoreboard(board: Mapping[str, Any], *, label: str = "") -> None:
    stage1 = board.get("stage1", {})
    stage3 = board.get("stage3", {})
    reliability = board.get("reliability", {})
    endpoint = board.get("end_to_end", {})
    weights = board.get("weights", {})

    print()
    print("=" * 80)
    print(f"SDOC SI-vs-BL scoreboard {label}".rstrip()
          + f"   |   n_emails = {board.get('n_emails')}")
    print("=" * 80)
    print("  权重    指标                                   数值")
    print("  ------  -------------------------------------  --------")
    rows = [
        (weights.get("stage1", 0.30), "Stage-1 macro-F1 (5 类)",
         stage1.get("macro_f1", 0.0)),
        (None, "Stage-1 accuracy", stage1.get("accuracy", 0.0)),
        (None, "Stage-1 rule_pct（规则占比，诊断项）", stage1.get("rule_pct") or 0.0),
        (weights.get("stage3", 0.20), "Stage-3 defect-F1", stage3.get("defect_f1", 0.0)),
        (None, "Stage-3 field-F1", stage3.get("field_f1", 0.0)),
        (None, "Stage-3 exact-match-rate", stage3.get("exact_match_rate", 0.0)),
        (None, "Stage-3 defect precision", stage3.get("defect_precision", 0.0)),
        (None, "Stage-3 defect recall", stage3.get("defect_recall", 0.0)),
        (weights.get("end_to_end", 0.50), "E2E 缺陷集合精确命中率",
         endpoint.get("rate", 0.0)),
        (None, "Reliability 升级召回", reliability.get("escalation_recall", 0.0)),
        (None, "Reliability 升级精确", reliability.get("escalation_precision", 0.0)),
    ]
    for weight, name, value in rows:
        tag = f"{weight:6.2f}" if weight is not None else "      "
        print(f"  {tag}  {name:38.38s}  {float(value):8.4f}  {_bar(float(value))}")

    print("-" * 80)
    success, total = endpoint.get("success"), endpoint.get("total")
    if total:
        print(f"  E2E 精确命中 {success}/{total} 封 —— 每封价值 {0.50 / total:.4f} 分")
    gold_review = reliability.get("gold_review", 0)
    if gold_review:
        per_reason = reliability.get("per_reason", {})
        caught = sum(int(reason.get("caught", 0)) for reason in per_reason.values())
        detail = "  ".join(f"{name}={reason.get('caught', 0)}/{reason.get('total', 0)}"
                           for name, reason in sorted(per_reason.items()))
        print(f"  人审升级 {caught}/{gold_review}   {detail}")
    print(f"  {'最终得分 FINAL SCORE':<44s}  {float(board.get('final_score', 0.0)):8.4f}"
          f"  {_bar(float(board.get('final_score', 0.0)))}")
    print("=" * 80)


def print_confusion(board: Mapping[str, Any], *, limit: int = 12) -> None:
    """打印 Stage-1 混淆矩阵的错分项（只打印非对角的）。"""
    confusion = board.get("stage1", {}).get("confusion") or {}
    errors: list[tuple[int, str, str]] = []
    for actual, predictions in confusion.items():
        for predicted, count in predictions.items():
            if actual != predicted and count:
                errors.append((int(count), actual, predicted))
    if not errors:
        print("  Stage-1 混淆矩阵无错分。")
        return
    errors.sort(reverse=True)
    print("  Stage-1 错分（实际 → 预测）：")
    for count, actual, predicted in errors[:limit]:
        print(f"    {count:4d}  {actual:15s} -> {predicted}")


def print_field_diagnosis(truth: Mapping[str, Any], submission: Mapping[str, Any],
                          *, limit: int = 15) -> None:
    """逐封对照 defect_fields，直接指出漏报/多报。"""
    missed: list[tuple[str, list[str], list[str]]] = []
    extra: list[tuple[str, list[str], list[str]]] = []
    for email_id, gold in truth.items():
        if gold.get("category") != "BL_COMPARISON" or gold.get("status") == "NEEDS_REVIEW":
            continue
        predicted = submission.get(email_id, {})
        gold_fields = set(gold.get("defect_fields") or [])
        predicted_fields = set(predicted.get("defect_fields") or [])
        if gold_fields == predicted_fields:
            continue
        if gold_fields - predicted_fields:
            missed.append((email_id, sorted(gold_fields - predicted_fields),
                           sorted(predicted_fields - gold_fields)))
        else:
            extra.append((email_id, sorted(predicted_fields - gold_fields), []))
    print(f"  漏报字段 (FN) {len(missed)} 封" + (f"，前 {limit}：" if missed else ""))
    for email_id, missing, surplus in missed[:limit]:
        print(f"    {email_id}: 漏 {missing} 多 {surplus}")
    print(f"  多报字段 (FP) {len(extra)} 封" + (f"，前 {limit}：" if extra else ""))
    for email_id, surplus, _ in extra[:limit]:
        print(f"    {email_id}: 多报 {surplus}")


# ---------------------------------------------------------------------------
# 快照与反回归
# ---------------------------------------------------------------------------
def best_score(report_dir: Path | None = None) -> float:
    path = Path(report_dir or DEFAULT_REPORT_DIR) / BEST_FILE_NAME
    if not path.is_file():
        return -1.0
    try:
        return float(json.loads(path.read_text(encoding="utf-8"))["final_score"])
    except (OSError, KeyError, ValueError, TypeError):
        return -1.0


def snapshot(
    board: Mapping[str, Any],
    submission: Mapping[str, Any],
    *,
    report_dir: Path | None = None,
    tag: str = "",
) -> Path:
    """落盘快照并维护 best.json —— 提交永远取历史最高分版本（反回归护栏）。"""
    directory = Path(report_dir or DEFAULT_REPORT_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    run_dir = directory / f"run_{stamp}{('_' + tag) if tag else ''}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "submission.json").write_text(
        json.dumps(dict(submission), indent=2, ensure_ascii=False), encoding="utf-8")
    (run_dir / "scoreboard.json").write_text(
        json.dumps(board, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    previous = best_score(directory)
    current = float(board.get("final_score", 0.0))
    if current > previous:
        (directory / BEST_FILE_NAME).write_text(json.dumps({
            "final_score": current, "run_dir": str(run_dir),
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "stage1_macro_f1": board.get("stage1", {}).get("macro_f1"),
            "stage3_defect_f1": board.get("stage3", {}).get("defect_f1"),
            "end_to_end": board.get("end_to_end", {}),
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  [best] 新峰值 {current:.4f}（历史 {previous:.4f}）→ {run_dir}")
    elif current == previous:
        # 持平（尤其是满分 1.0000）时不能报"低于峰值"：1.0000 已无更高可能，
        # 却在满分级反复提示"回退版本"，会让人以为当前版本坏了。
        print(f"  [best] 本次 {current:.4f} 与峰值持平（{previous:.4f}）—— 无需回退")
    else:
        print(f"  [best] 本次 {current:.4f} 低于峰值 {previous:.4f}"
              f" —— 提交时请回退到 best.json 指向的版本")
    return run_dir
