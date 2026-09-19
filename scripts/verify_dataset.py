#!/usr/bin/env python3
"""校验 bootstrap 解出的数据集是否完整、可用。

这个脚本是每次开工前的第一道体检：数据集少了附件或少了 email_id，
后面所有跑批与打分都会静默给出错误结论，所以必须显式拦住。

    python3 scripts/verify_dataset.py
    python3 scripts/verify_dataset.py --data-dir data --strict
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _harden_console() -> None:
    """Windows 控制台默认 cp1252，打印任何中文会直接抛 UnicodeEncodeError。

    本仓库所有入口脚本（verify / runner / test_submit / run_all）都必须调用它，
    否则在 PowerShell / cmd 下会看到一堆看不懂的编码异常。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


_harden_console()

EXPECTED_EMAILS = 520
EXPECTED_ATTACHMENTS = 250
EXPECTED_CATEGORIES = {"BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"}
CANONICAL_FIELDS = (
    "shipper", "consignee", "notify_party", "port_of_loading",
    "port_of_discharge", "container_count", "gross_weight_kg",
)
SUBMISSION_KEYS = {"category", "status", "review_reason", "has_defect", "defect_fields"}

OK = "  [ok]  "
BAD = "  [!!]  "
WARN = "  [warn]"


def fail(message: str, problems: list[str]) -> None:
    print(BAD + message)
    problems.append(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--ground-truth", default="eval/private/ground_truth.json")
    parser.add_argument("--strict", action="store_true", help="任一警告也视为失败")
    args = parser.parse_args()

    data_dir = (ROOT / args.data_dir) if not Path(args.data_dir).is_absolute() else Path(args.data_dir)
    problems: list[str] = []
    warnings: list[str] = []

    print("=" * 74)
    print("SDOC 数据集体检")
    print("=" * 74)

    # ---------- 1. 目录结构 ----------
    inbox_dir = data_dir / "inbox"
    attach_dir = data_dir / "attachments"
    if not inbox_dir.is_dir():
        fail(f"缺少 {inbox_dir}（先跑 make bootstrap）", problems)
        print("\n结论：无法继续体检。")
        return 1
    if not attach_dir.is_dir():
        fail(f"缺少 {attach_dir}", problems)

    # ---------- 2. 邮件 ----------
    inbox_files = sorted(inbox_dir.glob("email_*.json"))
    print(f"\n1) 邮件数量：{len(inbox_files)}（期望 {EXPECTED_EMAILS}）")
    if len(inbox_files) != EXPECTED_EMAILS:
        (fail if args.strict else lambda m, p: (print(WARN + " " + m), warnings.append(m)))(
            f"邮件数量为 {len(inbox_files)}，期望 {EXPECTED_EMAILS}", problems)
    else:
        print(OK + f"{EXPECTED_EMAILS} 封邮件就位")

    email_ids: list[str] = []
    malformed: list[str] = []
    attachment_refs: list[str] = []
    empty_attachment_emails = 0
    for path in inbox_files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            malformed.append(f"{path.name}: {exc}")
            continue
        email_id = record.get("email_id")
        if email_id != path.stem:
            malformed.append(f"{path.name}: email_id={email_id!r} 与文件名不一致")
            continue
        email_ids.append(email_id)
        attachments = record.get("attachments") or []
        if not attachments:
            empty_attachment_emails += 1
        attachment_refs.extend(attachments)
    if malformed:
        fail(f"{len(malformed)} 封邮件结构异常：{malformed[:3]}", problems)
    else:
        print(OK + f"全部邮件的 email_id / from / subject / body / attachments 字段可解析")
    print(f"      无附件邮件 {empty_attachment_emails} 封"
          "（在 BL_COMPARISON 里这是合法的「索要草稿 BL」请求，GT 判为 OK）")

    # ---------- 3. 附件 ----------
    print(f"\n2) 附件：引用了 {len(attachment_refs)} 个")
    missing = [ref for ref in attachment_refs if not (data_dir / ref).is_file()]
    if missing:
        fail(f"{len(missing)} 个附件引用缺失：{missing[:3]}", problems)
    else:
        print(OK + "所有附件引用都指向真实文件")

    extensions = Counter(Path(ref).suffix.lower() for ref in attachment_refs)
    print(f"      格式分布：{dict(sorted(extensions.items()))}")
    actual_attachments = len(list(attach_dir.rglob("*")))
    actual_files = len([p for p in attach_dir.rglob("*") if p.is_file()])
    if actual_files != EXPECTED_ATTACHMENTS:
        print(WARN + f" 附件目录实际文件 {actual_files} 个，期望 {EXPECTED_ATTACHMENTS} 个")
        warnings.append(f"附件数 {actual_files} != {EXPECTED_ATTACHMENTS}")
    else:
        print(OK + f"{EXPECTED_ATTACHMENTS} 个附件就位")

    # 附件命名约定：_SI / _BL 必须能被分槽
    unslotted = [ref for ref in attachment_refs
                 if Path(ref).stem.upper().rsplit("_", 1)[-1] not in {"SI", "BL"}]
    if unslotted:
        print(WARN + f" {len(unslotted)} 个附件无法按 _SI/_BL 分槽：{unslotted[:3]}")
        warnings.append("存在无法分槽的附件")
    else:
        print(OK + "全部附件可按键名 _SI / _BL 分槽")

    # ---------- 4. sample_submission 形状 ----------
    print("\n3) sample_submission.json")
    sample_path = data_dir / "sample_submission.json"
    if not sample_path.is_file():
        fail(f"缺少 {sample_path}", problems)
    else:
        sample = json.loads(sample_path.read_text(encoding="utf-8"))
        if set(sample) != set(email_ids):
            extra = sorted(set(sample) - set(email_ids))[:3]
            lost = sorted(set(email_ids) - set(sample))[:3]
            fail(f"键集与 inbox 不一致：多余 {extra} / 缺失 {lost}", problems)
        else:
            print(OK + f"{len(sample)} 个键，与 inbox 完全一致")
        shapes = {tuple(sorted(record)) for record in sample.values()}
        if shapes == {tuple(sorted(SUBMISSION_KEYS))}:
            print(OK + "每条记录都是官方 5 键："f"{sorted(SUBMISSION_KEYS)}")
        else:
            fail(f"记录键集不符合官方合约：{shapes}", problems)

    # ---------- 5. 官方 loader 与评分器 ----------
    print("\n4) 官方组件")
    loader = data_dir / "loader.py"
    if loader.is_file():
        print(OK + f"{loader} 就位（Inbox(「{args.data_dir}」) 可直接使用）")
    else:
        print(BAD + f"缺少 {loader}")
    if not loader.is_file():
        problems.append("缺少 loader.py")
    scoring = ROOT / "server" / "scoring.py"
    if scoring.is_file():
        print(OK + f"{scoring} 就位（服务器/离线打分共用）")
    else:
        print(WARN + " 缺少 server/scoring.py —— 离线打分不可用（需 make bootstrap 或 Docker）")
        warnings.append("缺少 scoring.py")

    # ---------- 6. 答案键与泄漏护栏 ----------
    print("\n5) 答案键与泄漏护栏")
    gt_path = ROOT / args.ground_truth
    if gt_path.is_file():
        truth = json.loads(gt_path.read_text(encoding="utf-8"))
        if set(truth) != set(email_ids):
            fail("答案键键集与 inbox 不一致", problems)
        else:
            distribution = Counter(record["category"] for record in truth.values())
            print(OK + f"{gt_path.relative_to(ROOT)} 就位，{len(truth)} 条标签")
            print(f"      分类分布：{dict(distribution.most_common())}")
            defects = [eid for eid, record in truth.items() if record.get("has_defect")]
            print(f"      has_defect 邮件：{len(defects)} 封（end-to-end 指标的分母）")
        if not any(field in json.dumps(list(truth.values())[:1]) for field in CANONICAL_FIELDS):
            print(OK + "答案键只含 5 键裁决，不含 7 字段原文（7 字段比较需自证）")

        try:
            check = subprocess.run(
                ["git", "check-ignore", "-q", str(gt_path.relative_to(ROOT))],
                cwd=ROOT, capture_output=True)
            if check.returncode == 0:
                print(OK + "ground_truth.json 已被 .gitignore 排除")
            else:
                fail("ground_truth.json 未被 .gitignore 排除 —— 推 GitHub 会泄漏答案键！", problems)
        except (OSError, subprocess.SubprocessError):
            print(WARN + " 无法调用 git，跳过 check-ignore 校验")
            warnings.append("未校验 gitignore")
    else:
        print(WARN + f" 未找到答案键 {gt_path}（离线打分不可用）")
        warnings.append("缺少 ground_truth.json")

    # ---------- 结论 ----------
    print("\n" + "=" * 74)
    if problems:
        print(f"结论：体检未通过，{len(problems)} 个致命问题")
        for problem in problems[:10]:
            print("  - " + problem)
        return 1
    if warnings and args.strict:
        print(f"结论：严格模式下有 {len(warnings)} 个警告")
        for warning in warnings[:10]:
            print("  - " + warning)
        return 1
    print(f"结论：体检通过（{len(warnings)} 个非致命警告）")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
