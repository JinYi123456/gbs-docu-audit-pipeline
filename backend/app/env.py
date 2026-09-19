"""`.env` 加载器（零依赖）。

★ 为什么必须有这个模块：
本项目的所有配置都从 `os.environ` 读（config.py / gemini.py / supabase.py /
ingest/inbox.py），但**没有任何地方把根目录的 .env 读进进程环境**。
结果是：你把真实的 GEMINI_API_KEY / SUPABASE_URL 写进 .env 之后，
`python -m app.runner` 依然认为「没有 API Key」，静默走规则通道 ——
AI 通道看起来"接好了"，实际从未被调用过。这个坑在演示台上是致命的
（评委问"你们的 AI 在哪"时，日志里连一次 API 调用记录都没有）。

设计取舍：
  · 不引入 python-dotenv（requirements 里没有，本地环境可能装不上）；
    解析规则只覆盖 .env 的合法子集，够用且完全可测。
  · **绝不覆盖**进程中已存在的环境变量：CI / 云端注入的变量优先级永远更高，
    这在 Vercel / Railway 上是必须的语义。
  · 缺文件、缺权限、行格式错误一律静默跳过 —— 配置层不能成为启动门槛。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final, Mapping

logger = logging.getLogger("sdoc.env")

# backend/app/env.py → 上溯三级 = 项目根
ROOT_DIR: Final[Path] = Path(__file__).resolve().parents[2]

DEFAULT_ENV_FILENAMES: Final[tuple[str, ...]] = (".env", ".env.local")

_QUOTES: Final[str] = "\"'"


def parse_env_text(text: str) -> dict[str, str]:
    """解析 .env 文本 → 键值对。

    支持的写法（够用即可，刻意不求全）：
        KEY=value
        KEY = "value with spaces"
        export KEY=value        # shell 粘贴友好
        KEY='value'             # 单引号：不做 # 截断
        KEY=value  # 行尾注释    # 仅当值未被引号包裹时截断
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
            value = value[1:-1]          # 引号内的 # 是内容，不做注释截断
        elif "#" in value:
            value = value.split("#", 1)[0].rstrip()
        values[key] = value
    return values


def load_env_file(
    path: str | Path | None = None,
    *,
    override: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    """把 .env 写进 os.environ（默认不覆盖已有变量）。返回实际写入的键值对。

    幂等：重复调用只会在第一次写入（因为默认不覆盖），适合在每个入口函数里都调一次。
    """
    target = Path(path) if path else ROOT_DIR / ".env"
    if not target.is_file():
        logger.debug("未找到 %s，跳过（云端部署走注入环境变量，属正常）", target)
        return {}
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:      # 权限/编码问题不该拖垮启动
        logger.warning("读取 %s 失败：%s", target, exc)
        return {}

    parsed = parse_env_text(text)
    store: Mapping[str, str] = env if env is not None else os.environ  # type: ignore[assignment]
    applied: dict[str, str] = {}
    for key, value in parsed.items():
        if not override and (os.environ.get(key) or "").strip():
            continue
        if env is not None:
            env[key] = value          # 测试用注入路径
        else:
            os.environ[key] = value
        applied[key] = value
    if applied:
        logger.debug("从 %s 载入 %d 个环境变量", target, len(applied))
    return applied


def load_project_env() -> dict[str, str]:
    """按优先级载入项目根目录的 .env / .env.local（先到先得，均不覆盖已存在变量）。"""
    applied: dict[str, str] = {}
    for name in DEFAULT_ENV_FILENAMES:
        for key, value in load_env_file(ROOT_DIR / name).items():
            applied.setdefault(key, value)
    return applied


def env_flag(name: str, default: str = "0") -> bool:
    return (os.environ.get(name, default) or "").strip().lower() not in {"0", "", "false", "no", "off"}


def env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":  # pragma: no cover —— 自查：只打印键名，绝不回显值
    import sys

    from .console import harden_console

    harden_console()
    loaded = load_project_env()
    print(f"根目录扫描：{ROOT_DIR}")
    for name in DEFAULT_ENV_FILENAMES:
        candidate = ROOT_DIR / name
        print(f"  {name:12} {'存在' if candidate.is_file() else '不存在'}")
    print(f"载入 {len(loaded)} 个变量（仅列键名）：")
    for key in sorted(loaded):
        print(f"  {key} = SET({len(loaded[key])} chars)")
    sys.exit(0)
