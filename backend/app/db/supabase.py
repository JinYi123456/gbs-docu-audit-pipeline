"""Supabase 云客户端（service-role）——线程安全、可降级、可重试。

三条硬约束：
  1. **导入安全**：本模块在没装 `supabase` SDK 的环境里也必须能 import。
     跑批与前端预览不允许因为一个后端依赖而整体崩掉（本地空跑是刚需）。
  2. **单例 + 线程安全**：SDK 的 Client 基于 httpx，可跨线程复用；
     但创建过程要用锁保护，避免并发首次调用创建出多个客户端。
  3. **阻塞调用绝不卡事件循环**：Supabase SDK 是同步的，一律走 asyncio.to_thread。

工程习惯：service-role key 只出现在后端进程里，永远不进前端 bundle、不进日志。
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Final, Mapping, TypeVar

from ..config import supabase_configured

logger = logging.getLogger("sdoc.supabase")

T = TypeVar("T")

# 可重试的瞬时故障：网络抖动 / 限流 / 网关错误
_RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524})


class SupabaseUnavailable(RuntimeError):
    """SDK 缺失 / 未配置 / 认证失败 —— 一律由此异常表达，调用方决定是否降级。"""


@dataclass(slots=True, frozen=True)
class SupabaseSettings:
    url: str = ""
    service_role_key: str = ""
    anon_key: str = ""
    schema: str = "public"
    timeout_s: float = 30.0
    max_attempts: int = 4
    base_backoff_s: float = 0.6

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SupabaseSettings":
        source = env if env is not None else os.environ
        return cls(
            url=(source.get("SUPABASE_URL") or "").strip().rstrip("/"),
            service_role_key=(source.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip(),
            anon_key=(source.get("SUPABASE_ANON_KEY") or "").strip(),
            schema=(source.get("SUPABASE_SCHEMA") or "public").strip() or "public",
            timeout_s=float(source.get("SUPABASE_TIMEOUT_S", "30")),
            max_attempts=int(source.get("SUPABASE_MAX_ATTEMPTS", "4")),
            base_backoff_s=float(source.get("SUPABASE_BACKOFF_S", "0.6")),
        )

    @property
    def configured(self) -> bool:
        return bool(self.url and self.service_role_key)

    def describe(self) -> dict[str, Any]:
        """可安全打印的配置摘要（绝不回显密钥）。"""
        return {
            "url": self.url or "(未配置)",
            "schema": self.schema,
            "service_role_key": "set" if self.service_role_key else "missing",
            "anon_key": "set" if self.anon_key else "missing",
            "timeout_s": self.timeout_s,
            "max_attempts": self.max_attempts,
        }


def sdk_available() -> bool:
    """supabase SDK 是否可导入（本地零依赖环境下为 False）。"""
    try:
        import supabase  # noqa: F401
    except Exception:  # noqa: BLE001 —— ImportError 与依赖链报错都算不可用
        return False
    return True


def _status_code(exc: BaseException) -> int | None:
    for attribute in ("status_code", "code", "status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def _retryable(exc: BaseException) -> bool:
    code = _status_code(exc)
    if code is not None:
        return code in _RETRYABLE_STATUS
    # 连接层异常（httpx / socket）没有 status_code，统一按可重试处理
    name = type(exc).__name__.lower()
    return any(token in name for token in ("timeout", "connect", "transport", "network", "read"))


class Gateway:
    """Supabase 访问入口。惰性建连，调用失败自动指数退避重试。"""

    def __init__(self, settings: SupabaseSettings | None = None) -> None:
        self.settings = settings or SupabaseSettings.from_env()
        self._client: Any | None = None
        self._lock = threading.Lock()
        self.stats: dict[str, int] = {"calls": 0, "retries": 0, "failures": 0}

    # -- 建连 ---------------------------------------------------------------
    @property
    def client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:          # double-checked locking
                return self._client
            if not self.settings.configured:
                raise SupabaseUnavailable(
                    "Supabase 未配置：请在 .env 设置 SUPABASE_URL 与 SUPABASE_SERVICE_ROLE_KEY")
            try:
                from supabase import create_client  # type: ignore
            except Exception as exc:  # noqa: BLE001
                raise SupabaseUnavailable(
                    f"未安装 supabase SDK（pip install supabase）：{exc}") from exc
            self._client = self._create_client(create_client)
            return self._client

    def _create_client(self, create_client: Any) -> Any:
        """建连，并**尽量**把超时真的传下去。

        坑：`SupabaseSettings.timeout_s` 早先只是声明了却从未生效 —— SDK 默认
        超时很宽，遇上云端抖动会让一次 upsert 卡住几十秒，500+ 行批量写时直接
        演变成"写入风暴"（每行都占着一个线程等超时）。
        SDK 各版本构造签名不一致（`options=` / `ClientOptions(timeout=)` /
        直接 `**kwargs`），因此这里用"探测式"调用：先试带 options，再试裸调用，
        都失败才放弃 —— 兼容性优先于优雅，且失败时只降级不崩。
        """
        timeout = max(1.0, float(self.settings.timeout_s))
        try:
            from supabase.client import ClientOptions  # type: ignore
        except Exception:  # noqa: BLE001 —— SDK 版本差异，探测失败属正常
            ClientOptions = None  # type: ignore[assignment]

        attempts: list[dict[str, Any]] = []
        if ClientOptions is not None:
            for kwargs in ({"postgrest_client_timeout": timeout, "storage_client_timeout": timeout},
                           {"postgrest_client_timeout": timeout}):
                try:
                    attempts.append({"options": ClientOptions(**kwargs)})
                except TypeError:
                    continue
        attempts.append({})

        last_error: BaseException | None = None
        for kwargs in attempts:
            try:
                return create_client(self.settings.url, self.settings.service_role_key, **kwargs)
            except TypeError as exc:      # 该版本不接受这组参数 → 换下一组
                last_error = exc
                continue
            except Exception as exc:  # noqa: BLE001
                raise SupabaseUnavailable(f"Supabase connection failed: {exc}") from exc
        raise SupabaseUnavailable(f"Supabase connection failed (incompatible parameters): {last_error}")

    # -- 表 / RPC -----------------------------------------------------------
    def table(self, name: str, *, schema: str | None = None) -> Any:
        endpoint = self.client.schema(schema or self.settings.schema)
        return endpoint.table(name)

    def rpc(self, name: str, params: Mapping[str, Any] | None = None) -> Any:
        return self.client.rpc(name, dict(params or {}))

    # -- 重试执行 -----------------------------------------------------------
    def run(self, operation: Callable[[], T], *, label: str = "supabase") -> T:
        """同步执行 + 指数退避重试（调用方负责放到线程里，别阻塞事件循环）。"""
        last: BaseException | None = None
        for attempt in range(1, max(1, self.settings.max_attempts) + 1):
            self.stats["calls"] += 1
            try:
                return operation()
            except SupabaseUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt >= self.settings.max_attempts or not _retryable(exc):
                    self.stats["failures"] += 1
                    raise
                self.stats["retries"] += 1
                delay = self.settings.base_backoff_s * (2 ** (attempt - 1))
                delay = delay * (0.7 + random.random() * 0.6)      # 加抖动，避免同步重试风暴
                logger.warning("%s 第 %d 次失败（%s），%.2fs 后重试：%s",
                               label, attempt, type(exc).__name__, delay, exc)
                time.sleep(delay)
        raise SupabaseUnavailable(f"{label} retries exhausted: {last}")

    async def arun(self, operation: Callable[[], T], *, label: str = "supabase") -> T:
        """异步包装：把同步 SDK 调用挪到线程池，绝不阻塞事件循环。

        外层再套一层硬超时：SDK 内部超时若因版本差异没生效，这里仍能兜住，
        避免 asyncio.gather 的一支永久悬挂（整批落库就永远不返回了）。
        超时按可重试处理一次都不重试地抛出 —— 由调用方（repo）决定是否降级。
        """
        budget = self._async_timeout_budget()
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.run, operation, label=label), timeout=budget)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            self.stats["failures"] += 1
            raise SupabaseUnavailable(
                f"{label} 超过硬超时 {budget:.0f}s（可能是云端限流或网络分区）") from exc

    def _async_timeout_budget(self) -> float:
        """一次 arun 的总预算 = 单次超时 × 尝试次数 + 退避开销，至少 10s。"""
        attempts = max(1, int(self.settings.max_attempts))
        backoff = self.settings.base_backoff_s * (2 ** attempts)
        return max(10.0, self.settings.timeout_s * attempts + backoff)

    # -- 健康检查 -----------------------------------------------------------
    def health(self, *, deep: bool = True) -> dict[str, Any]:
        report: dict[str, Any] = {
            "sdk_installed": sdk_available(),
            "configured": self.settings.configured,
            "settings": self.settings.describe(),
            "stats": dict(self.stats),
            "ok": False,
        }
        if not report["sdk_installed"] or not self.settings.configured:
            report["detail"] = "SDK 缺失或环境变量未配置（跑批仍可离线产出 submission.json）"
            return report
        if not deep:
            report["ok"] = True
            return report
        try:
            response = self.run(lambda: self.table("emails").select("email_id").limit(1).execute(),
                                label="health")
            report["ok"] = True
            report["rows"] = len(getattr(response, "data", []) or [])
        except Exception as exc:  # noqa: BLE001
            report["detail"] = f"{type(exc).__name__}: {exc}"
        return report

    async def ahealth(self, *, deep: bool = True) -> dict[str, Any]:
        return await asyncio.to_thread(self.health, deep=deep)


# ---------------------------------------------------------------------------
# 进程级单例（线程安全）
# ---------------------------------------------------------------------------
_GATEWAY: Gateway | None = None
_GATEWAY_LOCK = threading.Lock()


def get_gateway(settings: SupabaseSettings | None = None, *, refresh: bool = False) -> Gateway:
    """进程级单例。**没装 supabase SDK 时自动改用 httpx 直连 PostgREST。**

    这是实测踩出来的：本机环境从未安装过 `supabase`，于是 `--write-db` 一直在
    静默跳过，云端永远空 —— 而 repo 只是调用 `get_gateway()`，根本感知不到传输层。
    因此这里按环境能力选通道（惰性导入避免循环依赖）。
    """
    global _GATEWAY
    if refresh or _GATEWAY is None:
        with _GATEWAY_LOCK:
            if refresh or _GATEWAY is None:
                from .rest import build_gateway
                _GATEWAY = build_gateway(settings)
    return _GATEWAY


def reset_gateway() -> None:
    """测试用：丢弃单例。"""
    global _GATEWAY
    with _GATEWAY_LOCK:
        _GATEWAY = None


def gateway_status() -> dict[str, Any]:
    """给 runner / CLI 打印的一行状态（不建连，纯环境判断）。"""
    settings = SupabaseSettings.from_env()
    installed = sdk_available()
    configured = supabase_configured()
    return {
        "sdk_installed": installed,
        "configured": configured,
        "transport": "sdk" if installed else ("rest" if configured else "none"),
        "url": settings.url or "(未配置)",
    }


if __name__ == "__main__":  # pragma: no cover —— 自检：不装 SDK、不配密钥也必须能跑
    import json

    assert SupabaseSettings.from_env({}).configured is False
    assert SupabaseSettings.from_env(
        {"SUPABASE_URL": "https://x.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "k"}
    ).configured is True
    gw = get_gateway()
    report = gw.health()
    assert "sdk_installed" in report and "stats" in report
    # 重试策略：可重试 / 不可重试两类都要判对
    class _Err(Exception):
        def __init__(self, code: int) -> None:
            super().__init__(f"http {code}")
            self.status_code = code

    assert _retryable(_Err(503)) and _retryable(_Err(429))
    assert not _retryable(_Err(400)) and not _retryable(_Err(401))
    assert _retryable(TimeoutError("read timed out"))
    # 单例语义
    assert get_gateway() is get_gateway()
    print("supabase.py self-test OK:", json.dumps(report, ensure_ascii=False))
