"""PostgREST 直连通道 —— 不装 `supabase` SDK 也能真正读写云端。

★ 为什么必须有这个模块（实测踩到的坑）：
本机 Python 环境里**没有 `supabase` SDK**（`requirements.txt` 里写了，但从未安装）。
于是 `runner --write-db` 的落库调用一直被 `SupabaseUnavailable` 静默跳过：
submission.json 照常产出，云端却永远是空的 —— 而"集成云基础设施"恰恰是比赛红线。
`repo.py` 只依赖 `get_gateway()` 返回对象的形状，因此换一条传输层对上层完全透明：

    · 装了 SDK  → 走 SDK（功能最全）
    · 没装 SDK  → 走本模块（httpx 直接讲 PostgREST 协议）
    · 都没配好  → 抛 SupabaseUnavailable，由调用方降级（离线跑批不受影响）

协议细节（必须与 SDK 行为逐字对齐，否则会静默写坏数据）：
  1. **upsert** = `POST /rest/v1/<table>`，请求头必须带
     `Prefer: resolution=merge-duplicates`；少了它，主键冲突会变成 409 而不是覆盖。
  2. **on_conflict** 必须显式传给查询串（`?on_conflict=email_id,doc_type`），
     否则 PostgREST 只认主键，而子表的唯一约束恰好等于主键时看起来"能跑"，
     一旦约束与主键不一致就会插入重复行。
  3. **delete** 的过滤是在查询串里（`?email_id=in.("a","b")&status=eq.OPEN`）。
  4. **rpc** = `POST /rest/v1/rpc/<name>`，参数是 JSON body（注意：是 body，不是查询串）。
  5. 返回体形状必须带 `.data`（repo 用 `getattr(response, "data", None)` 取值）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .supabase import Gateway, SupabaseSettings, SupabaseUnavailable, sdk_available

logger = logging.getLogger("sdoc.supabase.rest")

# 单次 upsert 的返回策略：我们只需要"写成功"，不需要回传整行
# （100 行 × 19 列的 representation 会让响应体膨胀几十倍，纯浪费带宽）
_RETURN_MINIMAL = "return=minimal"
_RETURN_REPRESENTATION = "return=representation"


@dataclass(slots=True)
class RestResponse:
    """与 supabase SDK 的 APIResponse 保持最小兼容：只暴露 .data。"""

    data: Any = None


class _QueryBuilder:
    """链式查询构造器：只实现 repo.py / main.py 真正用到的那些方法。"""

    def __init__(self, gateway: "RestGateway", path: str) -> None:
        self._gateway = gateway
        self._path = path
        self._method = "GET"
        self._select = "*"
        self._filters: list[tuple[str, str]] = []
        self._orders: list[str] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._payload: Any = None
        self._on_conflict: str | None = None
        self._prefer: list[str] = []

    # -- 构造 ---------------------------------------------------------------
    def select(self, columns: str = "*") -> "_QueryBuilder":
        self._select = columns or "*"
        return self

    def order(self, column: str, *, desc: bool = False) -> "_QueryBuilder":
        self._orders.append(f"{column}.{'desc' if desc else 'asc'}")
        return self

    def limit(self, count: int) -> "_QueryBuilder":
        self._limit = int(count)
        return self

    def range(self, start: int, end: int) -> "_QueryBuilder":
        """与 SDK 语义一致：**闭区间** [start, end]。"""
        self._offset = int(start)
        self._limit = max(0, int(end) - int(start) + 1)
        return self

    def eq(self, column: str, value: Any) -> "_QueryBuilder":
        self._filters.append((column, f"eq.{_literal(value)}"))
        return self

    def in_(self, column: str, values: Iterable[Any]) -> "_QueryBuilder":
        rendered = ",".join(_quoted(value) for value in values)
        self._filters.append((column, f"in.({rendered})"))
        return self

    # -- 写入 ---------------------------------------------------------------
    def upsert(self, rows: Mapping[str, Any] | Sequence[Mapping[str, Any]],
               *, on_conflict: str | None = None) -> "_QueryBuilder":
        self._method = "POST"
        self._payload = [dict(rows)] if isinstance(rows, Mapping) else list(rows)
        self._on_conflict = on_conflict
        self._prefer.append("resolution=merge-duplicates")
        self._prefer.append(_RETURN_MINIMAL)
        return self

    def insert(self, rows: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> "_QueryBuilder":
        self._method = "POST"
        self._payload = [dict(rows)] if isinstance(rows, Mapping) else list(rows)
        self._prefer.append(_RETURN_REPRESENTATION)
        return self

    def delete(self) -> "_QueryBuilder":
        self._method = "DELETE"
        self._prefer.append(_RETURN_MINIMAL)
        return self

    # -- 执行 ---------------------------------------------------------------
    def params(self) -> dict[str, str]:
        query: dict[str, str] = {}
        if self._method == "GET":
            query["select"] = self._select
        for column, expression in self._filters:
            query[column] = expression
        if self._orders:
            query["order"] = ",".join(self._orders)
        if self._limit is not None:
            query["limit"] = str(self._limit)
        if self._offset is not None:
            # 注意：不能写 `if self._offset` —— offset=0 是合法值但为假值，
            # 早先版本因此在首页丢掉了分页参数（自检直接抓到，见文件末尾）
            query["offset"] = str(self._offset)
        if self._on_conflict:
            query["on_conflict"] = self._on_conflict
        return query

    def headers(self) -> dict[str, str]:
        headers = {}
        if self._prefer:
            headers["Prefer"] = ", ".join(dict.fromkeys(self._prefer))
        return headers

    def execute(self) -> RestResponse:
        client = self._gateway.client          # 未配置时在这里抛 SupabaseUnavailable
        kwargs: dict[str, Any] = {
            "params": self.params(),
            "headers": self.headers(),
        }
        if self._method != "DELETE" and self._payload is not None:
            kwargs["json"] = self._payload
        response = client.request(self._method, self._path, **kwargs)
        # ★★ 必须显式检查状态码：httpx **不会**自动对 4xx/5xx 抛异常。
        #    早先版本因此把"约束拒绝、密钥无效、权限不足"全部静默当成写成功 ——
        #    实测证据：故意插入违反 emails_verdict_ck 的行（MISMATCH + 空缺陷集），
        #    它居然"写入成功"，而云端根本没有任何约束报错记录。
        #    repo 的失败隔离与重试全靠异常，这里吞掉异常 = 静默丢数据。
        if response.status_code >= 400:
            raise _postgrest_error(response)
        return RestResponse(data=_decode(response))


def _literal(value: Any) -> str:
    """PostgREST 过滤值：字符串里的逗号/括号/句点必须加引号，否则会被当成语法。"""
    text = str(value)
    return text if text.replace("_", "").replace(".", "").isalnum() else _quoted(text)


def _quoted(value: Any) -> str:
    text = str(value).replace('"', '\\"')
    return f'"{text}"'


def _postgrest_error(response: Any) -> Exception:
    """把 PostgREST 错误包成带 status_code 的异常，让 _retryable() 能正确分流。

    `_status_code()` 会依次读异常的 status_code / code / status / response.status_code，
    因此继承 httpx.HTTPStatusError 最省事；但 httpx 可能未安装，故用 httpx 的异常
    构造器失败时退化为自建异常。
    """
    detail = _decode(response)
    if isinstance(detail, dict):
        message = f"{detail.get('code', '')} {detail.get('message', '')}".strip()
    else:
        message = str(detail)[:300]
    try:
        import httpx

        request = getattr(response, "request", None)
        if request is not None:
            return httpx.HTTPStatusError(f"PostgREST {response.status_code}: {message}",
                                         request=request, response=response)
    except Exception:  # noqa: BLE001 —— httpx 缺失或签名不符时不阻碍错误上报
        pass

    class PostgrestError(RuntimeError):
        def __init__(self, text: str, status_code: int) -> None:
            super().__init__(text)
            self.status_code = status_code

    return PostgrestError(f"PostgREST {response.status_code}: {message}", response.status_code)


def _decode(response: Any) -> Any:
    if response.status_code == 204 or not response.content:
        return []
    try:
        return response.json()
    except ValueError:
        return response.text[:500]


class RestGateway(Gateway):
    """用 httpx 直连 PostgREST 的网关。

    刻意继承 `Gateway` 以复用它的**重试、抖动退避、线程池包装、硬超时**逻辑 ——
    这些是踩坑换来的，重写一遍只会有新的坑。只有"传输"和"建连"被替换。
    """

    transport_name = "rest"

    def __init__(self, settings: SupabaseSettings | None = None) -> None:
        super().__init__(settings)
        self._http: Any | None = None

    # -- 建连（替代 SDK 的 create_client） -----------------------------------
    @property
    def client(self) -> Any:
        if not self.settings.configured:
            raise SupabaseUnavailable(
                "Supabase 未配置：请在 .env 设置 SUPABASE_URL 与 SUPABASE_SERVICE_ROLE_KEY")
        if self._http is not None:
            return self._http
        with self._lock:
            if self._http is None:
                self._http = self._build_http_client()
            return self._http

    def _build_http_client(self) -> Any:
        try:
            import httpx
        except ImportError as exc:      # pragma: no cover
            raise SupabaseUnavailable(
                f"REST 通道需要 httpx（pip install httpx）：{exc}") from exc
        url = self.settings.url.rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise SupabaseUnavailable(f"SUPABASE_URL 不是合法地址：{url!r}")
        # service-role key 只出现在服务端进程的请求头里，绝不进前端、绝不进日志
        headers = {
            "apikey": self.settings.service_role_key,
            "Authorization": f"Bearer {self.settings.service_role_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.settings.schema and self.settings.schema != "public":
            headers["Accept-Profile"] = self.settings.schema
            headers["Content-Profile"] = self.settings.schema
        return httpx.Client(base_url=url, headers=headers,
                            timeout=self.settings.timeout_s)

    # -- 表 / RPC -----------------------------------------------------------
    def table(self, name: str, *, schema: str | None = None) -> _QueryBuilder:
        target_schema = schema or self.settings.schema or "public"
        return _QueryBuilder(self, f"/rest/v1/{name}") if target_schema == "public" \
            else _QueryBuilder(self, f"/rest/v1/{name}")

    def rpc(self, name: str, params: Mapping[str, Any] | None = None) -> _QueryBuilder:
        builder = _QueryBuilder(self, f"/rest/v1/rpc/{name}")
        builder._method = "POST"                                   # noqa: SLF001 —— 同模块协作
        builder._payload = dict(params or {})                      # noqa: SLF001
        builder._prefer.append(_RETURN_REPRESENTATION)             # noqa: SLF001
        return builder

    # -- 健康检查 -----------------------------------------------------------
    def health(self, *, deep: bool = True) -> dict[str, Any]:
        report: dict[str, Any] = {
            "sdk_installed": sdk_available(),
            "transport": self.transport_name,
            "configured": self.settings.configured,
            "settings": self.settings.describe(),
            "stats": dict(self.stats),
            "ok": False,
        }
        if not self.settings.configured:
            report["detail"] = "环境变量未配置（跑批仍可离线产出 submission.json）"
            return report
        if not deep:
            report["ok"] = True
            return report
        try:
            response = self.run(
                lambda: self.table("emails").select("email_id").limit(1).execute(),
                label="health")
            report["ok"] = True
            report["rows"] = len(getattr(response, "data", []) or [])
        except Exception as exc:  # noqa: BLE001
            report["detail"] = f"{type(exc).__name__}: {exc}"
        return report


def build_gateway(settings: SupabaseSettings | None = None) -> Gateway:
    """按环境能力选传输层：有 SDK 用 SDK，没 SDK 用 REST（都能读能写）。"""
    if sdk_available():
        return Gateway(settings)
    gateway = RestGateway(settings)
    logger.info("未安装 supabase SDK —— 使用 httpx 直连 PostgREST 通道")
    return gateway


if __name__ == "__main__":  # pragma: no cover —— 自检：无网络、无 SDK 也要能验证装配
    settings = SupabaseSettings.from_env(
        {"SUPABASE_URL": "https://demo.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "k"})
    gateway = RestGateway(settings)
    builder = gateway.table("emails").upsert([{"email_id": "e1"}], on_conflict="email_id")
    assert builder._method == "POST"
    assert builder.params()["on_conflict"] == "email_id"
    assert "resolution=merge-duplicates" in builder.headers()["Prefer"]
    delete = gateway.table("comparisons").delete().in_("email_id", ["e1", "e2"]).eq("status", "OPEN")
    assert delete._method == "DELETE"
    assert delete.params()["email_id"] == 'in.("e1","e2")'
    assert delete.params()["status"] == "eq.OPEN"
    ranged = gateway.table("submission_view").select("email_id").order("email_id").range(0, 9)
    assert ranged.params()["offset"] == "0" and ranged.params()["limit"] == "10"
    assert gateway.rpc("apply_manual_verdict", {"p_email_id": "e1"}).params().get("select") is None
    unconfigured = RestGateway(SupabaseSettings.from_env({}))
    try:
        _ = unconfigured.client
    except SupabaseUnavailable as exc:
        assert "未配置" in str(exc)
    else:      # pragma: no cover
        raise AssertionError("未配置时必须抛 SupabaseUnavailable")
    print("rest.py self-test OK：PostgREST 通道装配正确（未联网）")
