"""集中配置。全部走环境变量，代码里不写死模型名与阈值。

刻意不依赖 pydantic-settings：本地环境可能没装，配置层不该成为启动门槛。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# 项目根目录（backend/app/config.py -> 上溯三级）
ROOT_DIR: Final[Path] = Path(__file__).resolve().parents[2]

# ---- 数据源 ----
DEFAULT_INBOX_SOURCE: Final[str] = "data"
DEFAULT_DATA_DIR: Final[str] = "data"

# ---- 官方合约常量（与官方 scoring.py 逐字一致，改动即破坏提交） ----
SUBMISSION_KEYS: Final[frozenset[str]] = frozenset(
    {"category", "status", "review_reason", "has_defect", "defect_fields"}
)
CATEGORIES: Final[tuple[str, ...]] = (
    "BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM",
)
STATUSES: Final[tuple[str, ...]] = ("OK", "MISMATCH", "NEEDS_REVIEW")
REVIEW_REASONS: Final[tuple[str, ...]] = (
    "wrong_doc_type", "missing_attachment", "unreadable", "missing_value",
)

# ---- 输出路径 ----
DEFAULT_SUBMISSION_PATH: Final[Path] = ROOT_DIR / "eval" / "report" / "submission.json"
DEFAULT_REPORT_DIR: Final[Path] = ROOT_DIR / "eval" / "report"
DEFAULT_GROUND_TRUTH: Final[Path] = ROOT_DIR / "eval" / "private" / "ground_truth.json"
DEFAULT_SCORING_MODULE: Final[Path] = ROOT_DIR / "server" / "scoring.py"
# 人审工作台读的快照（前端只依赖这一个文件，零密钥、断网可演示）
DEFAULT_DASHBOARD_PATH: Final[Path] = ROOT_DIR / "eval" / "report" / "dashboard.json"


@dataclass(slots=True, frozen=True)
class RuntimeSettings:
    """运行时配置快照。"""

    inbox_source: str = DEFAULT_INBOX_SOURCE
    data_dir: str = DEFAULT_DATA_DIR
    submit_url: str = "http://localhost:8080"
    concurrency: int = 6
    use_llm: bool = True
    write_db: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "RuntimeSettings":
        source = env if env is not None else dict(os.environ)
        return cls(
            inbox_source=source.get("INBOX_SOURCE", DEFAULT_INBOX_SOURCE),
            data_dir=source.get("INBOX_DATA_DIR", DEFAULT_DATA_DIR),
            submit_url=source.get("SUBMIT_URL", "http://localhost:8080"),
            concurrency=int(source.get("PIPELINE_CONCURRENCY", "6")),
            use_llm=source.get("USE_LLM", "1") != "0",
            write_db=source.get("WRITE_DB", "0") == "1",
        )


def resolve_data_dir(value: str | Path | None = None) -> Path:
    """把 data 目录解析为绝对路径。

    关键：官方数据集在**项目根**的 data/，而跑批常常从 backend/ 下启动。
    若直接用 cwd 相对路径，会误报「找不到 data/inbox」。
    """
    raw = Path(value) if value else Path(DEFAULT_DATA_DIR)
    if raw.is_absolute():
        return raw
    candidate = ROOT_DIR / raw
    if candidate.is_dir():
        return candidate
    return Path.cwd() / raw


def gemini_api_key_available(env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else dict(os.environ)
    return bool((source.get("GEMINI_API_KEY") or source.get("GOOGLE_API_KEY") or "").strip())


def supabase_configured(env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else dict(os.environ)
    return bool((source.get("SUPABASE_URL") or "").strip()
                and (source.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip())
