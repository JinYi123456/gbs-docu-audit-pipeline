"""「实时拉取新邮件」—— Demo 现场用的流式处理入口。

它做的事是**真的**：从数据源取下一批邮件，跑完整条链（分类 → 抽取 → 比对 → 裁决），
返回每封的结论与 Agent 轨迹。不是前端假动画。

为什么不做成"后台长驻 worker + WebSocket"：
  · demo 需要的是**可控节奏**（点一下、看它一封封变红/变绿），不是真实吞吐；
  · 同步返回 + 前端逐条错峰翻转，视觉上就是流式，且失败面最小（无连接管理）。
真实场景要换成常驻消费者时，替换本模块的 `pull_once` 即可，上层契约不变。

★ 红线约束：本模块**只读数据源，默认不写库**。
  写库必须显式 `persist_truth=True`（写的是与 submission 同源的权威结论），
  否则现场反复点按钮就可能污染 520 键的提交产物。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from itertools import cycle
from typing import Any, Sequence

from .agents.orchestrator import verify_email
from .ingest.inbox import InboxEmail, InboxSource

logger = logging.getLogger("sdoc.stream")

DEFAULT_BATCH = 6
MAX_BATCH = 24


@dataclass(slots=True)
class StreamItem:
    email_id: str
    subject: str
    from_addr: str
    attachments: list[str]
    category: str
    status: str
    has_defect: bool
    defect_fields: list[str]
    review_reason: str | None
    duration_ms: int
    trace: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "email_id": self.email_id, "subject": self.subject,
            "from": self.from_addr, "attachments": self.attachments,
            "category": self.category, "status": self.status,
            "has_defect": self.has_defect, "defect_fields": self.defect_fields,
            "review_reason": self.review_reason, "duration_ms": self.duration_ms,
            "trace": self.trace,
        }


@dataclass(slots=True)
class StreamBatch:
    items: list[StreamItem] = field(default_factory=list)
    cursor: int = 0
    pool_size: int = 0
    duration_ms: int = 0
    defect_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [item.as_dict() for item in self.items],
            "cursor": self.cursor, "pool_size": self.pool_size,
            "batch_size": len(self.items), "duration_ms": self.duration_ms,
            "defect_count": self.defect_count,
        }


class StreamCursor:
    """轮转游标：反复点按钮会依次看到不同的邮件（而不是同一批重复）。"""

    def __init__(self) -> None:
        self._position = 0

    @property
    def position(self) -> int:
        return self._position

    def advance(self, count: int, pool_size: int) -> int:
        start = self._position % max(1, pool_size)
        self._position = (start + max(0, count)) % max(1, pool_size)
        return start

    def reset(self) -> None:
        self._position = 0


CURSOR = StreamCursor()


def candidate_emails(source: InboxSource, *, limit: int | None = None) -> list[InboxEmail]:
    """可进入比对流程的邮件池：有附件的 BL 对照任务（即真正会产生红框的那批）。

    刻意不把 300 封非对照类邮件放进来：演示时点出一封 SPAM 不会有任何视觉信息。
    池子按 email_id 排序，保证多次拉取的顺序稳定、可复现。
    """
    from .pipeline.classify import CATEGORY_BL_COMPARISON, rule_classify

    pool: list[InboxEmail] = []
    for email in source.emails():
        if email.attachment_count == 0:
            continue
        result = rule_classify(email)
        if result is not None and result.category == CATEGORY_BL_COMPARISON:
            pool.append(email)
    pool.sort(key=lambda item: item.email_id)
    return pool[:limit] if limit else pool


async def pull_once(
    source: InboxSource,
    *,
    batch_size: int = DEFAULT_BATCH,
    client: Any | None = None,
    use_llm: bool = False,
    cursor: StreamCursor | None = None,
    concurrency: int = 4,
) -> StreamBatch:
    """取下一批邮件并跑完整条链（默认确定性通道：毫秒级，演示不卡）。"""
    started = time.perf_counter()
    size = max(1, min(int(batch_size), MAX_BATCH))
    pool = candidate_emails(source)
    if not pool:
        return StreamBatch(cursor=0, pool_size=0,
                           duration_ms=int((time.perf_counter() - started) * 1000))

    active = cursor or CURSOR
    start = active.advance(size, len(pool))
    # 环状取片：批尾自动绕回开头，连续点按钮不会出现空批
    rotated = list(pool[start:]) + list(pool[:start])
    window = rotated[:size]

    semaphore = asyncio.Semaphore(max(1, concurrency))
    results: list[StreamItem | None] = [None] * len(window)

    async def worker(index: int, email: InboxEmail) -> None:
        async with semaphore:
            try:
                outcome = await verify_email(email, source=source, client=client,
                                             use_llm=use_llm and client is not None)
            except Exception as exc:      # noqa: BLE001 —— 单封失败不中断本批
                logger.warning("拉取处理失败 %s：%s", email.email_id, exc)
                return
            record = outcome.record
            results[index] = StreamItem(
                email_id=email.email_id, subject=email.subject, from_addr=email.sender,
                attachments=[path.rsplit("/", 1)[-1] for path in email.attachments],
                category=record["category"], status=record["status"],
                has_defect=record["has_defect"], defect_fields=record["defect_fields"],
                review_reason=record["review_reason"],
                duration_ms=outcome.state.trace_summary()["total_ms"],
                trace=outcome.state.trace())

    await asyncio.gather(*(worker(index, email) for index, email in enumerate(window)))
    items = [item for item in results if item is not None]
    batch = StreamBatch(
        items=items, cursor=active.position, pool_size=len(pool),
        duration_ms=int((time.perf_counter() - started) * 1000),
        defect_count=sum(1 for item in items if item.has_defect))
    logger.info("拉取 %d 封（池 %d，游标 %d）：缺陷 %d，用时 %dms",
                len(items), batch.pool_size, batch.cursor, batch.defect_count,
                batch.duration_ms)
    return batch


def reset_cursor() -> None:
    CURSOR.reset()
