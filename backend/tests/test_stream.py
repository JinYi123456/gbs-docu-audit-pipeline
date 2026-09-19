"""实时拉取（stream.py）回归测试 —— 不经过 HTTP，直接测引擎。

盯四件事：
  1. 候选池只含**真正会产生红框**的邮件（有附件的 BL_COMPARISON），否则演示点出 SPAM 毫无信息；
  2. 游标是环状轮转的：连续拉取不重复，绕回后仍能取到；
  3. 单封失败不拖垮整批（异常隔离）；
  4. 拉取结果与跑批产物**结论一致** —— 否则现场会出现"拉出来是 MISMATCH、
     但拿去打分的那份是 OK"的致命不一致。

    python -m tests.test_stream
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from app.console import harden_console  # noqa: E402
from app.config import DEFAULT_SUBMISSION_PATH, resolve_data_dir  # noqa: E402
from app.ingest.inbox import InboxSource  # noqa: E402
from app.stream import (  # noqa: E402
    MAX_BATCH, StreamCursor, candidate_emails, pull_once, reset_cursor,
)


def _source() -> InboxSource:
    return InboxSource("data", data_dir=resolve_data_dir(None))


def _submission() -> dict[str, dict[str, object]]:
    return json.loads(DEFAULT_SUBMISSION_PATH.read_text(encoding="utf-8"))


def test_candidate_pool_is_scoped_to_comparable_emails() -> None:
    source = _source()
    pool = candidate_emails(source)
    assert pool, "候选池不能为空"
    assert all(email.attachment_count > 0 for email in pool)
    # 池子里全是 BL 对照类，且没有 SPAM/GENERAL 混入
    assert len(pool) == 126, len(pool)
    assert len({email.email_id for email in pool}) == len(pool), "池里不能有重复邮件"


def test_cursor_rotates_and_wraps() -> None:
    cursor = StreamCursor()
    assert cursor.advance(4, 10) == 0
    assert cursor.advance(4, 10) == 4
    assert cursor.advance(4, 10) == 8
    # 8 + 4 = 12 → 绕回首部，且不越界（下一个起点是 2）
    assert cursor.advance(4, 10) == 2
    assert cursor.position == 6
    cursor.reset()
    assert cursor.position == 0


def test_pull_once_returns_full_trace_without_duplicates() -> None:
    reset_cursor()
    source = _source()
    batch = asyncio.run(pull_once(source, batch_size=5, cursor=StreamCursor()))
    assert batch.pool_size == 126
    assert len(batch.items) == 5
    assert len({item.email_id for item in batch.items}) == 5
    for item in batch.items:
        assert item.status in ("OK", "MISMATCH", "NEEDS_REVIEW")
        assert item.category == "BL_COMPARISON"
        assert len(item.trace) == 4
        assert [step["role"] for step in item.trace] == ["triage", "extractor", "verifier", "judge"]
        assert item.duration_ms >= 0
        # 红框必须来自同一批字段：defect ⇔ verifier 的 evidence.defect_fields
        # （缺一侧的邮件走升级阶梯，verifier 证据里没有该键 —— 用 get 容忍）
        assert set(item.defect_fields) == {
            step_field for step in item.trace if step["role"] == "verifier"
            for step_field in step["evidence"].get("defect_fields", [])}
    assert batch.defect_count == sum(1 for item in batch.items if item.has_defect)


def test_pull_agrees_with_official_submission() -> None:
    """★ 红线：现场拉出来的结论必须与提交产物逐字符一致。"""
    submission = _submission()
    batch = asyncio.run(pull_once(_source(), batch_size=12, cursor=StreamCursor()))
    for item in batch.items:
        submitted = submission[item.email_id]
        assert submitted["status"] == item.status, item.email_id
        assert sorted(submitted["defect_fields"]) == sorted(item.defect_fields), item.email_id
        assert bool(submitted["has_defect"]) == item.has_defect, item.email_id


def test_batch_size_is_clamped() -> None:
    """前端传个离谱的 batch_size 也不能把演示拖死。"""
    batch = asyncio.run(pull_once(_source(), batch_size=999, cursor=StreamCursor()))
    assert len(batch.items) <= MAX_BATCH


def test_single_failure_does_not_kill_the_batch() -> None:
    """一封抛异常，其余照常返回（演示时不能整批白屏）。

    ★ 刻意用 try/finally 自己打补丁，而**不用 pytest 的 monkeypatch fixture**：
    `tests.run_all` 是直接调用测试函数的（不经过 pytest 的 fixture 注入），
    依赖 fixture 会让这条测试在一键入口里报 "missing argument" 假失败。
    """
    source = _source()
    pool = candidate_emails(source)
    broken_id = pool[0].email_id

    import app.stream as stream_module

    real = stream_module.verify_email

    async def flaky(email, **kwargs):      # noqa: ANN001, ANN003, ANN202
        if email.email_id == broken_id:
            raise RuntimeError("模拟单封失败")
        return await real(email, **kwargs)

    stream_module.verify_email = flaky
    try:
        batch = asyncio.run(stream_module.pull_once(source, batch_size=3, cursor=StreamCursor()))
    finally:
        stream_module.verify_email = real
    assert broken_id not in {item.email_id for item in batch.items}
    assert len(batch.items) == 2


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
