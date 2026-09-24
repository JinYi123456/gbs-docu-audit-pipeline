"""Gemini 客户端：Schema 护栏 + 指数退避 + 内容寻址缓存。

三件套保证结构化输出不会轻易失败：
  1. 启动期 assert_schema_is_gemini_safe() 拦住带默认值的 schema
  2. 请求失败按类型分流：永久错误立即抛，瞬时错误指数退避 + 抖动重试
  3. 主模型不可用（模型改名/下线）→ 自动降级到 GEMINI_MODEL_FALLBACK

并发模型：一个进程一个 Client，配 asyncio.run 的单事件循环使用；
并发由 Semaphore 限流，不依赖 SDK 自身的线程安全假设。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping, Sequence, TypeVar

from pydantic import BaseModel, ValidationError

from ..env import env_float

logger = logging.getLogger("sdoc.gemini")

DEFAULT_MODEL_CLASSIFY: Final[str] = "gemini-2.5-flash"
DEFAULT_MODEL_EXTRACT: Final[str] = "gemini-2.5-pro"
DEFAULT_MAX_RETRIES: Final[int] = 5
DEFAULT_BASE_DELAY: Final[float] = 1.0
DEFAULT_MAX_DELAY: Final[float] = 32.0
# 2.5-pro 的思考 token 计入输出预算 —— 给小了会截断 JSON
DEFAULT_MAX_OUTPUT_TOKENS: Final[int] = 8192
# 单次调用的硬超时。为什么必须有：2.5-pro 在思考模式下偶尔会"想很久"，
# 没有这一层时整批会被一封邮件拖住（Semaphore 名额被占满 → 全批停摆）。
DEFAULT_REQUEST_TIMEOUT_S: Final[float] = 120.0

# ---------------------------------------------------------------------------
# 模型候选链 —— 这是本项目踩过的最贵的一个坑的解法。
#
# 事实（2026-09 实测，同一个 API Key）：
#   · `models.list()` **列出的模型不一定能调**：2.5-pro / 2.5-flash 在列表里，
#     但 generateContent 返回 404 "no longer available to new users"。
#   · 免费档对 pro 系列的输入配额是 **limit: 0** —— 调用必然 429，重试再多次也没用。
#   · 热门 flash 型号会返回 503 UNAVAILABLE（高负载），过一会儿又能用。
#
# 因此：**绝不把模型名写死在业务代码里**。启动时按候选链探活 → 缓存结果 →
# 调用时失败自动换下一个型号。Google 随时会下线型号，这条路让代码活得比型号久。
# ---------------------------------------------------------------------------
DEFAULT_CLASSIFY_CHAIN: Final[tuple[str, ...]] = (
    "gemini-3.1-flash-lite",      # 最省 token 的档，分类这种短任务最合适
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-flash-latest",
    "gemini-2.5-flash",
)
DEFAULT_EXTRACT_CHAIN: Final[tuple[str, ...]] = (
    "gemini-3.6-flash",            # 多模态 + 结构化输出实测可用
    "gemini-3.5-flash",
    "gemini-flash-latest",
    "gemini-3.1-pro-preview",      # 有付费配额时它最强，额度耗尽会自动跳过
    "gemini-2.5-pro",
)
# 模型探活结果的缓存时长（小时）。Google 下线型号是突发性的，24h 重探一次足够。
DEFAULT_PLAN_TTL_H: Final[float] = 24.0
PLAN_CACHE_FILE: Final[str] = "model_plan.json"

_TRANSIENT_MARKERS: Final[tuple[str, ...]] = (
    "429", "500", "502", "503", "504", "resource_exhausted", "resource exhausted",
    "unavailable", "deadline_exceeded", "deadline exceeded", "internal error",
    "overloaded", "rate limit", "quota", "timeout", "connection",
)
_PERMANENT_MARKERS: Final[tuple[str, ...]] = (
    "invalid_argument", "invalid argument", "not_found", "not found",
    "permission_denied", "permission denied", "unauthenticated", "api key",
    "failed_precondition", "400", "401", "403", "404",
)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

# 任务类型（决定用哪条模型候选链）
TASK_CLASSIFY: Final[str] = "classify"
TASK_EXTRACT: Final[str] = "extract"


def _model_unavailable(text: str) -> bool:
    """判断一个错误是否属于"这个型号用不了"（换型号就能救）而不是"请求本身有问题"。

    三类都要认：
      · 404 / not_found        —— 型号已下线（Google 对新用户禁用旧型号时会这样）
      · limit: 0 / free tier   —— 免费档对该型号额度为 0（pro 系列常见）
      · permission_denied      —— 该 key 无此型号权限
    """
    lowered = text.lower()
    if any(token in lowered for token in ("not_found", "404", "not found", "permission_denied")):
        return True
    if "limit: 0" in lowered or "limit: 0," in lowered:
        return True
    return "resource_exhausted" in lowered and "free_tier" in lowered and "limit: 0" in lowered


def _chain(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    """解析 `GEMINI_*_CANDIDATES`（逗号分隔），与内置链合并去重、保持顺序。"""
    extra = tuple(part.strip() for part in (raw or "").split(",") if part.strip())
    seen: list[str] = []
    for name in (*extra, *default):
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


class GeminiError(RuntimeError):
    pass


class GeminiConfigError(GeminiError):
    pass


class GeminiTransientError(GeminiError):
    pass


class GeminiPermanentError(GeminiError):
    pass


class GeminiSchemaError(GeminiError):
    pass


# ---------------------------------------------------------------------------
# Schema 护栏
# ---------------------------------------------------------------------------
def find_default_paths(node: Any, path: str = "$") -> list[str]:
    """递归找出 JSON Schema 里所有 default 键的位置（Gemini 不支持任何默认值）。"""
    leaks: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "default":
                leaks.append(path)
            leaks.extend(find_default_paths(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            leaks.extend(find_default_paths(value, f"{path}[{index}]"))
    return leaks


def assert_schema_is_gemini_safe(model: type[BaseModel]) -> None:
    """任何默认值都会在请求时炸，这里提前拦死。"""
    leaks = find_default_paths(model.model_json_schema())
    if leaks:
        raise GeminiSchemaError(
            f"{model.__name__} 含默认值（Gemini 会拒绝：Default value is not supported）："
            f"{leaks[:8]}。请改为 Optional[X]（不带 = None），宽松化交给本地校验器。"
        )


def verify_all_schemas() -> None:
    from .schemas import SCHEMA_REGISTRY
    for name, model in SCHEMA_REGISTRY.items():
        assert_schema_is_gemini_safe(model)
        logger.debug("schema %s 通过默认值护栏", name)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class GeminiSettings:
    api_key: str
    model_classify: str = DEFAULT_MODEL_CLASSIFY
    model_extract: str = DEFAULT_MODEL_EXTRACT
    model_fallback: str = ""
    classify_thinking_budget: int = 0    # flash 可关思考：分类更快更省
    extract_thinking_budget: int = -1    # -1 = 交给模型默认（pro 不能关思考）
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    max_retries: int = DEFAULT_MAX_RETRIES
    base_delay: float = DEFAULT_BASE_DELAY
    max_delay: float = DEFAULT_MAX_DELAY
    concurrency: int = 8
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    cache_enabled: bool = True
    cache_dir: str = ".cache/gemini"
    classify_candidates: tuple[str, ...] = DEFAULT_CLASSIFY_CHAIN
    extract_candidates: tuple[str, ...] = DEFAULT_EXTRACT_CHAIN
    model_probe: bool = True          # 启动时真调一次探活（结果缓存，成本可忽略）
    plan_ttl_h: float = DEFAULT_PLAN_TTL_H

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GeminiSettings":
        source = env if env is not None else dict(os.environ)
        api_key = (source.get("GEMINI_API_KEY") or source.get("GOOGLE_API_KEY") or "").strip()
        if not api_key:
            raise GeminiConfigError("missing GEMINI_API_KEY (or GOOGLE_API_KEY)")

        def _int(name: str, default: int) -> int:
            try:
                return int(str(source.get(name, default)).strip())
            except (TypeError, ValueError):
                return default

        def _float(name: str, default: float) -> float:
            try:
                return float(str(source.get(name, default)).strip())
            except (TypeError, ValueError):
                return default

        return cls(
            api_key=api_key,
            model_classify=source.get("GEMINI_MODEL_CLASSIFY", DEFAULT_MODEL_CLASSIFY),
            model_extract=source.get("GEMINI_MODEL_EXTRACT", DEFAULT_MODEL_EXTRACT),
            model_fallback=source.get("GEMINI_MODEL_FALLBACK", ""),
            classify_thinking_budget=_int("GEMINI_CLASSIFY_THINKING", 0),
            extract_thinking_budget=_int("GEMINI_EXTRACT_THINKING", -1),
            max_output_tokens=_int("GEMINI_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS),
            max_retries=_int("GEMINI_MAX_RETRIES", DEFAULT_MAX_RETRIES),
            concurrency=_int("GEMINI_CONCURRENCY", 8),
            request_timeout_s=max(5.0, env_float("GEMINI_TIMEOUT_S", DEFAULT_REQUEST_TIMEOUT_S)),
            cache_enabled=source.get("GEMINI_CACHE", "1") != "0",
            cache_dir=source.get("GEMINI_CACHE_DIR", ".cache/gemini"),
            classify_candidates=_chain(source.get("GEMINI_CLASSIFY_CANDIDATES"),
                                       DEFAULT_CLASSIFY_CHAIN),
            extract_candidates=_chain(source.get("GEMINI_EXTRACT_CANDIDATES"),
                                      DEFAULT_EXTRACT_CHAIN),
            model_probe=source.get("GEMINI_MODEL_PROBE", "1") != "0",
            plan_ttl_h=max(0.0, _float("GEMINI_PLAN_TTL_H", DEFAULT_PLAN_TTL_H)),
        )

    def chain_for(self, task: str) -> tuple[str, ...]:
        return self.extract_candidates if task == "extract" else self.classify_candidates


@dataclass(slots=True)
class LLMUsage:
    calls: int = 0
    cache_hits: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    failures: int = 0
    latency_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls, "cache_hits": self.cache_hits, "retries": self.retries,
            "prompt_tokens": self.prompt_tokens, "output_tokens": self.output_tokens,
            "failures": self.failures, "latency_ms": round(self.latency_ms, 1),
        }


# ---------------------------------------------------------------------------
# 内容寻址缓存
# ---------------------------------------------------------------------------
class LLMCache:
    """改一次提示词重跑 520 封几乎零成本；也让同一输入逐比特可重放。"""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def set(self, key: str, payload: dict[str, Any]) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:      # 缓存失败绝不能影响主流程
            logger.debug("缓存写入失败：%s", exc)


# ---------------------------------------------------------------------------
# 模型探活与缓存
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ModelPlan:
    """本次进程实际可用的模型型号（探活结果，落盘缓存）。"""

    classify: str = ""
    extract: str = ""
    checked_at: str = ""
    probed: dict[str, str] = field(default_factory=dict)   # 型号 -> "ok" / 错误简写

    def model_for(self, task: str) -> str:
        return self.extract if task == TASK_EXTRACT else self.classify

    def as_dict(self) -> dict[str, Any]:
        return {"classify": self.classify, "extract": self.extract,
                "checked_at": self.checked_at, "probed": dict(self.probed)}


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
class GeminiClient:
    def __init__(self, settings: GeminiSettings, *, cache: LLMCache | None = None) -> None:
        self._settings = settings
        self._client: Any | None = None
        self._semaphore = asyncio.Semaphore(max(1, settings.concurrency))
        self.usage = LLMUsage()
        self.cache = cache or (LLMCache(settings.cache_dir) if settings.cache_enabled else None)
        self._plan: ModelPlan | None = None
        self._plan_lock = asyncio.Lock()
        verify_all_schemas()

    @property
    def settings(self) -> GeminiSettings:
        return self._settings

    def sdk(self) -> tuple[Any, Any]:
        """返回 (client, types)。惰性导入，未安装 google-genai 时才报错。"""
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:      # pragma: no cover
            raise GeminiConfigError("google-genai is not installed; run `pip install google-genai`") from exc
        if self._client is None:
            self._client = genai.Client(api_key=self._settings.api_key)
        return self._client, types

    # -- 模型探活 -----------------------------------------------------------
    @property
    def plan_path(self) -> Path:
        return Path(self._settings.cache_dir) / PLAN_CACHE_FILE

    def cached_plan(self) -> ModelPlan | None:
        """读探活缓存（超过 TTL 或字段缺失则视为无效）。"""
        path = self.plan_path
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            checked_at = str(payload.get("checked_at") or "")
            if not checked_at:
                return None
            age_h = (time.time() - datetime.fromisoformat(checked_at).timestamp()) / 3600.0
            if age_h > self._settings.plan_ttl_h:
                logger.info("模型探活缓存已过期（%.1fh），重新探测", age_h)
                return None
            return ModelPlan(
                classify=str(payload.get("classify") or ""),
                extract=str(payload.get("extract") or ""),
                checked_at=checked_at, probed=dict(payload.get("probed") or {}))
        except (OSError, ValueError) as exc:
            logger.debug("模型探活缓存不可用：%s", exc)
            return None

    def save_plan(self, plan: ModelPlan) -> None:
        try:
            self.plan_path.parent.mkdir(parents=True, exist_ok=True)
            self.plan_path.write_text(json.dumps(plan.as_dict(), ensure_ascii=False, indent=2),
                                      encoding="utf-8")
        except OSError as exc:      # 缓存失败绝不能影响主流程
            logger.debug("模型探活缓存写入失败：%s", exc)

    async def ensure_models(self, *, force: bool = False) -> ModelPlan:
        """保证拿到一组**真实可调**的模型型号（每个进程只算一次）。

        为什么必须有这一步：`models.list()` 会列出已经调不通的型号（实测 404），
        写死的型号名会在某天静默报废。这里用**真实的小请求**逐号探活，
        第一个答得出来的即为该任务的型号；结果落盘，TTL 内不再重复探。
        """
        if self._plan is not None and not force:
            return self._plan
        async with self._plan_lock:
            if self._plan is not None and not force:
                return self._plan
            if not force:
                cached = self.cached_plan()
                if cached is not None:
                    if not cached.classify:
                        cached.classify = cached.extract or self._settings.model_classify
                    if not cached.extract:
                        cached.extract = cached.classify or self._settings.model_extract
                    self._plan = cached
                    logger.info("模型探活缓存命中：classify=%s extract=%s",
                                cached.classify, cached.extract)
                    return cached
            if not self._settings.model_probe:
                self._plan = ModelPlan(
                    classify=self._settings.model_classify,
                    extract=self._settings.model_extract,
                    checked_at=datetime.now(timezone.utc).isoformat(),
                    probed={"mode": "probe-disabled"})
                return self._plan
            self._plan = await self._probe_models()
            self.save_plan(self._plan)
            return self._plan

    async def _probe_models(self) -> ModelPlan:
        """逐号探活。顺序：先配置的主/备模型，再走候选链。"""
        from .schemas import ClassifyOut

        classify_chain = self._ordered_candidates(TASK_CLASSIFY)
        extract_chain = self._ordered_candidates(TASK_EXTRACT)
        unique: list[str] = []
        for name in (*classify_chain, *extract_chain):
            if name and name not in unique:
                unique.append(name)

        probed: dict[str, str] = {}
        alive: set[str] = set()
        for name in unique:
            try:
                await self._ping(name, ClassifyOut)
                probed[name] = "ok"
                alive.add(name)
                logger.info("模型探活通过：%s", name)
            except Exception as exc:  # noqa: BLE001 —— 探活失败不是致命错误
                probed[name] = f"{type(exc).__name__}: {str(exc)[:120]}"
                logger.warning("模型探活失败：%s → %s", name, probed[name])

        classify = next((name for name in classify_chain if name in alive), "")
        extract = next((name for name in extract_chain if name in alive), "")
        # 交叉兜底：某一档全部不可用时，用另一档顶上，绝不让 AI 通道空转
        classify = classify or extract or ""
        extract = extract or classify or ""
        plan = ModelPlan(classify=classify or self._settings.model_classify,
                         extract=extract or self._settings.model_extract,
                         checked_at=datetime.now(timezone.utc).isoformat(), probed=probed)
        logger.warning("模型探活完成：classify=%s extract=%s（共测 %d 个型号）",
                       plan.classify, plan.extract, len(unique))
        return plan

    async def _ping(self, model: str, schema: type[SchemaT]) -> None:
        """最小成本的真实调用（约 16 tokens 输出），只验证"这个型号能答结构化输出"。"""
        client, types = self.sdk()
        config = types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=schema,
            temperature=0.0, max_output_tokens=256,
            thinking_config=types.ThinkingConfig(thinking_budget=0))
        probe_prompt = (
            "Ping. Return a JSON object matching the schema. Example email: "
            "SUBJECT: REQUEST BL DRAFT _ PO 26000_ UNCOATED WOODFREE PAPER IN REA_ "
            "FROM: ops@aprilasia.com BODY: Attached the SI and the draft BL, please compare.")
        timeout = min(45.0, max(10.0, self._settings.request_timeout_s))
        await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model, contents=[probe_prompt], config=config),
            timeout=timeout)

    def _ordered_candidates(self, task: str) -> list[str]:
        """候选顺序：环境配置的主模型 → 配置的降级模型 → 候选链（去重）。"""
        primary = (self._settings.model_extract if task == TASK_EXTRACT
                   else self._settings.model_classify)
        head: list[str] = []
        if task == TASK_EXTRACT:
            head = [primary, self._settings.model_extract, self._settings.model_classify]
        else:
            head = [primary, self._settings.model_classify, self._settings.model_fallback]
        if self._settings.model_fallback:
            head.append(self._settings.model_fallback)
        ordered: list[str] = []
        for name in (*head, *self._settings.chain_for(task)):
            if name and name not in ordered:
                ordered.append(name)
        return ordered

    async def candidates_for(self, task: str) -> list[str]:
        """实际调用用的候选序列：**探活命中的那个排第一**，其余按原顺序跟随。

        重要：探活已知不可用的型号（404 / limit:0）**不再放进候选序列**。
        否则每封邮件都要先撞一次 404 才换人 —— 单次很快，但 520 封 × 每次失败
        都是白白增加延迟与日志噪声，而且对"死型号"的退避重试完全是浪费。
        只有探活整体没跑（probe 关闭或全部失败）时才退化为全链尝试。
        """
        plan = await self.ensure_models()
        chain = self._ordered_candidates(task)
        alive = {name for name, status in plan.probed.items() if status == "ok"}
        if alive:
            filtered = [name for name in chain if name in alive]
            if filtered:
                chain = filtered
        best = plan.model_for(task)
        if best and best in chain:
            chain.remove(best)
        elif best and alive:
            # 探活命中的型号不在本文任务的链上（例如跨档兜底）——仍然优先用它
            return [best] + [name for name in chain if name != best]
        return ([best] if best else []) + chain

    # -- 缓存键 --------------------------------------------------------------
    def cache_key(self, model: str, schema_name: str, system_instruction: str,
                  payload: Sequence[Any]) -> str:
        digest = hashlib.sha256()
        digest.update(model.encode())
        digest.update(b"\x00")
        digest.update(schema_name.encode())
        digest.update(b"\x00")
        digest.update(hashlib.sha256(system_instruction.encode()).hexdigest().encode())
        for item in payload:
            digest.update(b"\x00")
            if isinstance(item, bytes):
                digest.update(hashlib.sha256(item).hexdigest().encode())
            else:
                digest.update(str(item).encode("utf-8", errors="replace"))
        return digest.hexdigest()

    # -- 主入口 --------------------------------------------------------------
    async def generate_structured(
        self,
        *,
        schema: type[SchemaT],
        contents: Sequence[Any],
        system_instruction: str,
        model: str,
        task: str = TASK_CLASSIFY,
        thinking_budget: int = -1,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
        cache_payload: Sequence[Any] | None = None,
    ) -> tuple[SchemaT, bool]:
        """返回 (校验后的对象, 是否命中缓存)。

        `model` 仍然是调用方声明的"首选型号"，但实际候选序列由探活计划决定 ——
        即：首选型号已经下线时，这里会自动换成探活命中的那个，而不是直接失败。
        """
        assert_schema_is_gemini_safe(schema)

        candidates = await self.candidates_for(task)
        if model and model in candidates:
            candidates.remove(model)
            candidates.insert(0, model)
        if not candidates:
            raise GeminiConfigError("model candidate chain is empty; check GEMINI_*_CANDIDATES config")

        payload_for_cache = list(cache_payload) if cache_payload is not None else [
            getattr(item, "text", None) or str(item) for item in contents
        ]
        # ★ 缓存键用**任务角色**而不是具体型号：
        #   否则 Google 下线一个型号、探活换到另一个，520 封的缓存就全部作废，
        #   白花一遍全量 token。提示词没改、输入没改，就该命中同一份缓存。
        key = self.cache_key(f"{task}:{schema.__name__}", "role",
                             system_instruction, payload_for_cache)
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                self.usage.cache_hits += 1
                try:
                    return schema.model_validate(cached), True
                except ValidationError:
                    logger.warning("缓存内容无法通过 %s 校验，忽略缓存", schema.__name__)

        last_error: BaseException | None = None
        for candidate in candidates:
            try:
                raw = await self._call_with_backoff(
                    model=candidate, schema=schema, contents=contents,
                    system_instruction=system_instruction, thinking_budget=thinking_budget,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens or self._settings.max_output_tokens,
                )
            except GeminiPermanentError as exc:
                if _model_unavailable(str(exc)):
                    last_error = exc
                    logger.warning("模型 %s 不可用（%s），尝试链上的下一个型号",
                                   candidate, str(exc)[:90])
                    continue
                raise
            except Exception as exc:      # noqa: BLE001
                last_error = exc
                continue
            if self.cache is not None:
                self.cache.set(key, raw)
            return self._validate_loose(schema, raw), False

        self.usage.failures += 1
        raise GeminiTransientError(
            f"候选链上 {len(candidates)} 个模型全部失败（链：{candidates}）：{last_error!r}")

    async def _call_with_backoff(
        self,
        *,
        model: str,
        schema: type[SchemaT],
        contents: Sequence[Any],
        system_instruction: str,
        thinking_budget: int,
        temperature: float,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        client, types = self.sdk()
        delay = self._settings.base_delay
        attempts = max(1, self._settings.max_retries)
        last_error: BaseException | None = None

        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                config = self._build_config(
                    types=types, schema=schema, system_instruction=system_instruction,
                    thinking_budget=thinking_budget, temperature=temperature,
                    max_output_tokens=max_output_tokens)
                async with self._semaphore:
                    # 硬超时：超时按**瞬时错误**处理（会退避重试），而不是让整批卡住。
                    # 注意这里只能超时、不能取消 SDK 内部的连接，所以超时后仍会
                    # 释放 Semaphore 名额 —— 这正是我们要的：单封慢邮件不许拖死全批。
                    response = await asyncio.wait_for(
                        client.aio.models.generate_content(
                            model=model, contents=list(contents), config=config),
                        timeout=self._settings.request_timeout_s)
                self.usage.calls += 1
                self.usage.latency_ms += (time.perf_counter() - started) * 1000
                self._account(response)
                return self._extract_json(response)
            except Exception as exc:      # noqa: BLE001
                last_error = exc
                detail = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:400]
                if self._classify(exc) is GeminiPermanentError:
                    logger.error("模型 %s 永久失败：%s", model, detail)
                    raise GeminiPermanentError(detail) from exc
                if attempt >= attempts:
                    break
                retry_after = self._retry_after(exc)
                wait = retry_after if retry_after is not None else min(
                    self._settings.max_delay, delay * (2 ** (attempt - 1)))
                wait *= 0.7 + random.random() * 0.6      # 抖动，避免并发惊群
                self.usage.retries += 1
                logger.warning("模型 %s 第 %d/%d 次失败（%s），%.1fs 后退避重试",
                               model, attempt, attempts, detail, wait)
                await asyncio.sleep(wait)

        raise GeminiTransientError(
            f"模型 {model} 重试 {attempts} 次仍失败：{type(last_error).__name__}: {last_error}")

    def _account(self, response: Any) -> None:
        metadata = getattr(response, "usage_metadata", None)
        if metadata is None:
            return
        self.usage.prompt_tokens += int(getattr(metadata, "prompt_token_count", 0) or 0)
        self.usage.output_tokens += int(getattr(metadata, "candidates_token_count", 0) or 0
                                        ) + int(getattr(metadata, "thoughts_token_count", 0) or 0)

    @staticmethod
    def _build_config(*, types: Any, schema: type[BaseModel], system_instruction: str,
                      thinking_budget: int, temperature: float, max_output_tokens: int) -> Any:
        kwargs: dict[str, Any] = {
            "system_instruction": system_instruction,
            "temperature": temperature,
            "response_mime_type": "application/json",
            "response_schema": schema,
            "max_output_tokens": max_output_tokens,
        }
        if thinking_budget >= 0:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _extract_json(response: Any) -> dict[str, Any]:
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, BaseModel):
            return parsed.model_dump()
        if isinstance(parsed, dict):
            return parsed
        text = (getattr(response, "text", None) or "").strip()
        if not text:
            raise GeminiSchemaError("empty response (possibly consumed by thinking tokens; raise GEMINI_MAX_OUTPUT_TOKENS)")
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GeminiSchemaError(f"response is not valid JSON: {text[:200]}") from exc
        if not isinstance(payload, dict):
            raise GeminiSchemaError(f"response is not a JSON object: {text[:200]}")
        return payload

    @staticmethod
    def _validate_loose(schema: type[SchemaT], payload: dict[str, Any]) -> SchemaT:
        """本地宽松化：只保留已知键，剔除模型幻觉出来的多余字段。"""
        known = set(schema.model_fields)
        return schema.model_validate({key: value for key, value in payload.items()
                                      if key in known})

    @staticmethod
    def _retry_after(exc: BaseException) -> float | None:
        match = re.search(r"retry[_ ]?(?:delay|in)[^0-9]{0,12}(\d+(?:\.\d+)?)\s*s",
                          str(exc), re.I)
        if match:
            try:
                return min(DEFAULT_MAX_DELAY, float(match.group(1)))
            except ValueError:
                return None
        return None

    @staticmethod
    def _classify(exc: BaseException) -> type[GeminiError]:
        # asyncio.wait_for 抛出的是裸 TimeoutError，str() 为空，必须显式判类型
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return GeminiTransientError
        text = f"{type(exc).__name__}: {exc}".lower()
        # ★ 关键："这个型号用不了"必须判为**永久错误**，而不是可重试。
        #   否则 pro 系列 limit:0 的 429 会让每封邮件白等 5 次退避重试（约 30s+），
        #   520 封就是几小时的纯浪费；判成永久后立刻换链上的下一个型号。
        if _model_unavailable(text):
            return GeminiPermanentError
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if isinstance(status, int) and 400 <= status < 500 and status != 429:
            return GeminiPermanentError
        if any(marker in text for marker in _PERMANENT_MARKERS) and "429" not in text:
            return GeminiPermanentError
        if any(marker in text for marker in _TRANSIENT_MARKERS):
            return GeminiTransientError
        return GeminiTransientError


_CLIENT: GeminiClient | None = None


def get_client(settings: GeminiSettings | None = None) -> GeminiClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = GeminiClient(settings or GeminiSettings.from_env())
    return _CLIENT


def reset_client() -> None:
    global _CLIENT
    _CLIENT = None
