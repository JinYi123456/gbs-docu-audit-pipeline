"""SDOC Hackathon 2026 · SI vs BL 智能校验后端。

★ 这里做一件看起来很小、但决定"AI 通道到底有没有被调用"的事：
  在**任何子模块被导入之前**把项目根目录的 .env 载入 os.environ。

为什么必须放在包初始化里、而不是 config.py：
  · `app/ingest/inbox.py` 在**模块导入时**就读取 os.environ（DEFAULT_SOURCE 等常量），
    如果加载动作晚于它的导入，那些常量就已经用旧值定死了；
  · 放在包子模块导入前，则无论谁先被导入（runner / main / 测试），顺序都正确。

云端（Vercel / Railway）没有 .env 文件也不会出问题：加载器缺文件即静默返回，
且**永不覆盖**平台注入的环境变量。
"""
from __future__ import annotations

from .env import load_project_env

__version__ = "0.2.0"

# 幂等；缺文件时是 no-op
try:      # 配置层绝不能成为启动门槛
    load_project_env()
except Exception:  # noqa: BLE001 —— 极端环境下（只读盘/编码异常）也要能 import
    pass
