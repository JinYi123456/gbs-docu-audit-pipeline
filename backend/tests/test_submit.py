"""自评测提交脚本 —— 把 submission 打到官方评分服务并打印 5 项指标。

四种跑法（按需要挑）：
    # 全量跑批 + 落库 + 提交
    python -m tests.test_submit --source db --tag d3

    # 直接提交本地产物文件（最快，适合反复刷分）
    python -m tests.test_submit --source file

    # Docker 没起也能验分：用官方 scoring.py 在本地打分
    python -m tests.test_submit --source file --offline

    # 带 decided_by 诊断（官方 score_stage1 会算出 rule_pct，演示时很有用）
    python -m tests.test_submit --source db --with-diagnostics

三条纪律（写进脚本里，避免 deadline 前手忙脚乱）：
  1. 提交前一定先过合约自检 —— 键集不全或状态不自洽，官方会把缺失键当 GENERAL 记罚；
  2. 每份提交都落盘留档（eval/report/submitted_*.json），出问题能逐字回溯；
  3. 每次打分都与历史峰值比对（best.json），低于峰值就别提交这一版。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from app.config import (  # noqa: E402
    DEFAULT_REPORT_DIR, DEFAULT_SUBMISSION_PATH, RuntimeSettings, SUBMISSION_KEYS,
)
from app.console import harden_console  # noqa: E402
from app.eval_support import (  # noqa: E402
    best_score, load_ground_truth, print_confusion, print_field_diagnosis, print_scoreboard,
    score_offline, snapshot,
)


# ---------------------------------------------------------------------------
# 读取产物
# ---------------------------------------------------------------------------
def load_from_file(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        raise SystemExit(f"产物不存在：{path}\n先跑 `python -m app.runner --write-db` 或 `--out {path}`")
    return json.loads(path.read_text(encoding="utf-8"))


def load_from_db(*, limit: int | None = None) -> dict[str, dict[str, Any]]:
    """从 Supabase 的 submission_view 读（人工改判已自动生效）。"""
    from app.db.repo import fetch_submission
    from app.db.supabase import SupabaseUnavailable

    try:
        return fetch_submission(limit=limit)
    except SupabaseUnavailable as exc:
        raise SystemExit(
            f"无法从 Supabase 读取：{exc}\n"
            f"改用 `--source file`（读 {DEFAULT_SUBMISSION_PATH}）或先配好 .env") from exc


# ---------------------------------------------------------------------------
# 提交
# ---------------------------------------------------------------------------
def post_submission(payload: dict[str, Any], *, url: str, timeout: float = 120.0) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    endpoint = url.rstrip("/") + "/submit"
    request = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise SystemExit(f"官方服务返回 HTTP {exc.code}：{detail}\n"
                         f"（服务没起？先 `docker compose up --build`）") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"连不上 {endpoint}：{exc.reason}\n"
            f"  1) docker compose up --build\n"
            f"  2) 或加 --offline 用本地 scoring.py 打分") from exc


def check_contract(
    submission: dict[str, dict[str, Any]],
    *,
    expected_ids: list[str] | None,
) -> list[str]:
    from app.db.repo import validate_submission

    problems = validate_submission(submission, expected_ids=expected_ids)
    if expected_ids is None:
        official_sample = ROOT / "data" / "sample_submission.json"
        if official_sample.is_file():
            sample = json.loads(official_sample.read_text(encoding="utf-8"))
            missing = sorted(set(sample) - set(submission))
            if missing:
                problems.append(
                    f"相比官方 sample_submission.json 缺 {len(missing)} 个键：{missing[:5]}")
    return problems


def main(argv: list[str] | None = None) -> int:
    harden_console()
    settings = RuntimeSettings.from_env()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=("file", "db", "auto"), default="file",
                        help="auto：能连 Supabase 就读视图（人工改判生效），否则退回本地产物")
    parser.add_argument("--file", type=Path, default=DEFAULT_SUBMISSION_PATH)
    parser.add_argument("--url", default=settings.submit_url)
    parser.add_argument("--offline", action="store_true",
                        help="不连官方服务，用本地 server/scoring.py 打分")
    parser.add_argument("--with-diagnostics", action="store_true",
                        help="附带 decided_by（官方会算出 rule_pct）")
    parser.add_argument("--expected", type=int, default=520,
                        help="期望的 email_id 数量（0 = 不检查）")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--tag", default="")
    parser.add_argument("--no-snapshot", action="store_true")
    args = parser.parse_args(argv)

    # ---- 1) 取产物 ----
    source = args.source
    if source == "auto":
        from app.db.supabase import SupabaseSettings, sdk_available
        source = ("db" if sdk_available() and SupabaseSettings.from_env().configured
                  else "file")
        print(f"[auto] 数据源自动选择 → {source}")
    if source == "file":
        submission = load_from_file(args.file)
    else:
        try:
            submission = load_from_db(limit=args.limit)
        except SystemExit as exc:      # auto 模式下云端不可用 → 安静退回文件
            if args.source != "auto":
                raise
            print(f"[auto] 云端不可用，退回本地产物：{str(exc).splitlines()[0]}")
            source = "file"
            submission = load_from_file(args.file)
    raw_count = len(submission)
    submission = {email_id: dict(record) for email_id, record in sorted(submission.items())}
    if not args.with_diagnostics:
        for record in submission.values():
            record.pop("decided_by", None)
    print(f"载入 submission：{raw_count} 条（source={source}）")

    # ---- 2) 合约自检（发车前必过）----
    expected_ids: list[str] | None = None
    if args.expected:
        try:
            expected_ids = sorted(load_ground_truth())
        except Exception:      # noqa: BLE001 —— 没有答案键就退化为只用 sample 检查
            expected_ids = None
        if expected_ids and len(expected_ids) != args.expected:
            print(f"  [warn] 答案键 {len(expected_ids)} 条 != --expected {args.expected}")
    problems = check_contract(submission, expected_ids=expected_ids)
    if problems:
        print(f"\n[合约自检未通过] {len(problems)} 个问题：")
        for problem in problems[:20]:
            print(f"  - {problem}")
        print("\n拒绝提交：官方会把缺失/非法键直接记罚，先修数据再打。")
        return 2
    keys = {key for record in submission.values() for key in record}
    print(f"合约自检通过：{len(submission)} 条，键集 {sorted(keys)}")

    # ---- 3) 打分 ----
    if args.offline:
        board = score_offline(submission)
        print("\n[offline] 使用本地 server/scoring.py 打分（等价于官方 /submit）")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        archive = Path(DEFAULT_REPORT_DIR) / f"submitted_{stamp}.json"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(json.dumps(submission, indent=2, ensure_ascii=False),
                           encoding="utf-8")
        print(f"提交产物已留档：{archive}")
        board = post_submission(submission, url=args.url)
        print(f"[live] 已 POST {args.url.rstrip('/')}/submit")

    label = f"({source}{'/offline' if args.offline else '/live'})"
    print_scoreboard(board, label=label)

    if not args.offline:
        try:
            truth = load_ground_truth()
            print("\n错分与字段诊断：")
            print_confusion(board)
            print_field_diagnosis(truth, submission)
        except Exception:      # noqa: BLE001
            pass
    elif expected_ids:
        print("\n错分与字段诊断：")
        print_confusion(board)
        print_field_diagnosis(load_ground_truth(), submission)

    previous = best_score()
    if not args.no_snapshot:
        snapshot(board, submission, tag=args.tag or ("offline" if args.offline else "live"))
    final = float(board.get("final_score", 0.0))
    # 三档而不是两档：`final == previous` 曾被归到"低于峰值，勿提交此版"，
    # 于是在满分状态下反复打印"勿提交"这种吓人的结论（1.0000 本来就无法再高）。
    if final > previous:
        verdict = "（新峰值，可以提交）"
    elif final == previous:
        verdict = "（与峰值持平，可以提交）"
    else:
        verdict = f"（低于峰值，勿提交此版；回退到 {previous:.4f}）"
    print(f"\n历史峰值 {previous:.4f} → 本次 {final:.4f} {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
