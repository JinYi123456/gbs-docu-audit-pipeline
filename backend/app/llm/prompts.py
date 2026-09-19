"""提示词装载与版本锁。

版本号进缓存键与 DB 的 prompt_version 列 —— 改提示词必须同步升版本，
否则内容寻址缓存会静默复用旧结果，你会以为改了但分数没变。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

PROMPT_DIR: Path = Path(os.environ.get("PROMPT_DIR", Path(__file__).parent / "prompts"))
CLASSIFY_PROMPT_FILE = "classify.system.md"
EXTRACT_PROMPT_FILE = "extract.system.md"

PROMPT_VERSION: str = os.environ.get("PROMPT_VERSION", "v3")


@lru_cache(maxsize=8)
def _read(filename: str) -> str:
    path = PROMPT_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"提示词文件缺失：{path}")
    return path.read_text(encoding="utf-8")


def classify_prompt() -> str:
    return _read(CLASSIFY_PROMPT_FILE)


def extract_prompt(mode: str, doc_a: str, doc_b: str = "") -> str:
    template = _read(EXTRACT_PROMPT_FILE)
    return (template.replace("{{MODE}}", mode)
            .replace("{{DOC_A}}", doc_a)
            .replace("{{DOC_B}}", doc_b))


def versions() -> dict[str, str]:
    return {
        "prompt_version": PROMPT_VERSION,
        "classify": f"classify@{PROMPT_VERSION}",
        "extract": f"extract@{PROMPT_VERSION}",
    }
