"""Windows 控制台是 cp1252，打印任何非 ASCII 都会直接抛 UnicodeEncodeError。

这是本机实测踩到的真实故障：`print("中文")` 在 cmd / PowerShell 下报
`'charmap' codec can't encode characters`。所有打印非 ASCII 的入口脚本
（verify_dataset / runner / test_submit / run_all）都必须先调用 harden_console()。
"""
from __future__ import annotations

import sys


def harden_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
