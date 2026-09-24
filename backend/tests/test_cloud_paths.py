"""Cloud-path integration tests — the two production incidents lived here uncovered.

Regression guards added after the Railway incidents of 2026-09-24:
  1. `fetch_emails_page` must map DB columns to display rows correctly
     (emails.verdict_status -> "status", attachment basenames, override flag);
  2. `_cloud_pull` must filter the replay pool like the local pipeline does
     (BL_COMPARISON + attachments only), wrap the cursor around the pool edge
     via modular indexing, and replay the *effective* (manual-override-aware)
     verdict — with a bounded window (never an unbounded `list(cycle(...))`);
  3. the audit endpoints (`/api/emails`, `/api/emails/{id}`) must fall back to
     the cloud page when no local snapshot exists, applying overrides first.

All Supabase traffic is stubbed with an in-memory fake gateway — no network.

    python -m tests.test_cloud_paths
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from app.console import harden_console  # noqa: E402

harden_console()

import app.main as main_module  # noqa: E402
import app.db.repo as repo_module  # noqa: E402
from app.db.repo import fetch_emails_page  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


# ---------------------------------------------------------------------------
# In-memory fake gateway: same builder-chaining surface the real one exposes
# ---------------------------------------------------------------------------
def _row(email_id: str, *, category: str = "BL_COMPARISON", status: str = "OK",
         attachments: list[str] | None = None, defects: list[str] | None = None,
         review_reason: str | None = None, manual_status: str | None = None,
         manual_reason: str | None = None, manual_defects: list[str] | None = None) -> dict[str, Any]:
    return {
        "email_id": email_id, "subject": f"subject-{email_id}", "from_addr": "sender@example.com",
        "attachment_paths": attachments if attachments is not None else [f"att/{email_id}_SI.txt", f"att/{email_id}_BL.txt"],
        "category": category, "verdict_status": status, "has_defect": status == "MISMATCH",
        "defect_fields": defects or [], "review_reason": review_reason,
        "manual_category": None, "manual_status": manual_status,
        "manual_review_reason": manual_reason, "manual_defect_fields": manual_defects,
        "classified_by": "rule",
    }


def _make_gateway(rows: list[dict[str, Any]]) -> Any:
    class FakeResponse:
        def __init__(self, data: list[dict[str, Any]]) -> None:
            self.data = data

    class FakeBuilder:
        def __init__(self, source: list[dict[str, Any]]) -> None:
            self._source = source

        def select(self, *_args: Any, **_kwargs: Any) -> "FakeBuilder":
            return self

        def in_(self, column: str, values: list[Any]) -> "FakeBuilder":
            self._source = [row for row in self._source if row.get(column) in set(values)]
            return self

        def order(self, column: str, *_args: Any, **_kwargs: Any) -> "FakeBuilder":
            self._source = sorted(self._source, key=lambda row: str(row.get(column)))
            return self

        def range(self, *_args: Any, **_kwargs: Any) -> "FakeBuilder":
            return self  # the fake dataset is tiny; paging is exercised via in_()

        def execute(self) -> FakeResponse:
            return FakeResponse(list(self._source))

    class FakeGateway:
        def run(self, operation: Any, *, label: str = "fake") -> Any:
            return operation()

        def table(self, _name: str) -> FakeBuilder:
            return FakeBuilder(rows)

    return FakeGateway()


def _install_gateway(monkey_rows: list[dict[str, Any]]) -> Any:
    gateway = _make_gateway(monkey_rows)
    repo_module.get_gateway = lambda: gateway  # type: ignore[assignment]
    return gateway


# ---------------------------------------------------------------------------
# 1. fetch_emails_page: DB column -> display row mapping
# ---------------------------------------------------------------------------
def test_fetch_emails_page_maps_verdict_status_and_basenames() -> None:
    original = repo_module.get_gateway
    try:
        _install_gateway([
            _row("email_004", status="MISMATCH", defects=["consignee"]),
        ])
        page = fetch_emails_page(email_ids=["email_004"])
        assert set(page) == {"email_004"}
        row = page["email_004"]
        assert row["status"] == "MISMATCH"          # emails.verdict_status -> status
        assert row["has_defect"] is True
        assert row["defect_fields"] == ["consignee"]
        # attachment basenames, not internal paths
        assert row["attachments"] == ["email_004_SI.txt", "email_004_BL.txt"]
        assert row["attachment_count"] == 2
        assert row["overridden"] is False
    finally:
        repo_module.get_gateway = original          # type: ignore[assignment]


def test_fetch_emails_page_flags_manual_overrides() -> None:
    original = repo_module.get_gateway
    try:
        _install_gateway([
            _row("email_010", manual_status="OK", manual_reason=None, manual_defects=[]),
        ])
        row = fetch_emails_page(email_ids=["email_010"])["email_010"]
        assert row["overridden"] is True
        assert row["manual_status"] == "OK"
    finally:
        repo_module.get_gateway = original          # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 2. _cloud_pull: pool filter, modular wrap-around, bounded window, overrides
# ---------------------------------------------------------------------------
def test_cloud_pull_filters_pool_and_wraps_cursor() -> None:
    original_ready = main_module._supabase_ready
    original_gateway = repo_module.get_gateway
    try:
        # pool: only BL_COMPARISON rows WITH attachments; email_006 has none,
        # email_015 is SPAM (no attachments either) — both must be excluded.
        rows = [
            _row("email_004", status="MISMATCH", defects=["consignee"]),
            _row("email_005", status="OK"),
            _row("email_006", status="OK", attachments=[]),
            _row("email_007", status="OK"),
        ]
        _install_gateway(rows)
        main_module._supabase_ready = lambda: True  # type: ignore[assignment]

        from app.stream import CURSOR
        CURSOR.reset()

        payload = asyncio.run(main_module._cloud_pull(batch_size=10))
        assert payload["channel"] == "cloud-replay"
        assert payload["read_only"] is True
        assert payload["pool_size"] == 3            # 004/005/007 — not 006
        # batch larger than pool must wrap around WITHOUT exploding (regression:
        # list(cycle(...)) OOM-killed the Railway container with HTTP 502)
        assert payload["batch_size"] == 10
        ids = [item["email_id"] for item in payload["items"]]
        assert len(ids) == 10
        assert set(ids) == {"email_004", "email_005", "email_007"}
        # deterministic modular order starting at the reset cursor
        assert ids[:4] == ["email_004", "email_005", "email_007", "email_004"]
        # archived verdicts replayed verbatim
        mismatched = [item for item in payload["items"] if item["email_id"] == "email_004"]
        assert all(item["status"] == "MISMATCH" and item["defect_fields"] == ["consignee"]
                   for item in mismatched)
        # every item carries a 4-step replay trace
        assert all(len(item["trace"]) == 4 for item in payload["items"])
    finally:
        main_module._supabase_ready = original_ready            # type: ignore[assignment]
        repo_module.get_gateway = original_gateway              # type: ignore[assignment]
        from app.stream import CURSOR
        CURSOR.reset()


def test_cloud_pull_replays_manual_verdict_over_archive() -> None:
    original_ready = main_module._supabase_ready
    original_gateway = repo_module.get_gateway
    try:
        # archived MISMATCH but a human later overturned it to OK via review
        rows = [_row("email_020", status="MISMATCH", defects=["shipper"],
                     manual_status="OK", manual_reason=None, manual_defects=[])]
        _install_gateway(rows)
        main_module._supabase_ready = lambda: True  # type: ignore[assignment]

        from app.stream import CURSOR
        CURSOR.reset()
        payload = asyncio.run(main_module._cloud_pull(batch_size=1))
        item = payload["items"][0]
        assert item["status"] == "OK"
        assert item["has_defect"] is False
        assert item["defect_fields"] == []
        # the judge step must mention the manual review
        judge_step = payload["items"][0]["trace"][3]
        assert "Manually reviewed" in judge_step["summary"]
    finally:
        main_module._supabase_ready = original_ready            # type: ignore[assignment]
        repo_module.get_gateway = original_gateway              # type: ignore[assignment]
        from app.stream import CURSOR
        CURSOR.reset()


# ---------------------------------------------------------------------------
# 3. Audit endpoints: cloud fallback when no local snapshot exists
# ---------------------------------------------------------------------------
def test_emails_endpoint_falls_back_to_cloud_with_overrides_applied() -> None:
    original_ready = main_module._supabase_ready
    original_gateway = repo_module.get_gateway
    try:
        rows = [
            _row("email_004", status="MISMATCH", defects=["consignee", "notify_party"]),
            _row("email_030", manual_status="OK"),      # overridden: archive said MISMATCH
        ]
        _install_gateway(rows)
        main_module._supabase_ready = lambda: True  # type: ignore[assignment]

        # hide the local snapshot so _local_dashboard takes the cloud branch
        real_load = main_module.load_dashboard
        main_module.load_dashboard = lambda *a, **k: None  # type: ignore[assignment]

        client = TestClient(main_module.app, raise_server_exceptions=False)
        body = client.get("/api/emails", params={"limit": 10}).json()
        by_id = {item["email_id"]: item for item in body["items"]}
        assert body["total"] == 2
        assert by_id["email_004"]["status"] == "MISMATCH"
        assert by_id["email_030"]["status"] == "OK"     # manual override wins
        assert by_id["email_030"]["overridden"] is True

        detail = client.get("/api/emails/email_004").json()
        assert detail["email"]["email_id"] == "email_004"
        assert detail["email"]["status"] == "MISMATCH"
        assert detail["email"]["fields"] == []          # field-level grid empty in cloud mode
        assert detail["submission"]["defect_fields"] == ["consignee", "notify_party"]

        missing = client.get("/api/emails/email_999999")
        assert missing.status_code == 404
    finally:
        main_module._supabase_ready = original_ready            # type: ignore[assignment]
        repo_module.get_gateway = original_gateway              # type: ignore[assignment]
        main_module.load_dashboard = real_load                  # type: ignore[assignment]


# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
