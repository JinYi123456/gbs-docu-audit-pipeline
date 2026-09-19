"""零依赖测试运行器 —— `make test` 走这里。

为什么不用 pytest：本地环境常常没装 pytest（这个仓库开发时就遇到过），
而"必须能跑测试"是纪律，不能依赖安装。全部用例都写成 `test_*` 函数，
pytest 装了就照常识别（两个入口并存），没装也能一键跑完。

    python -m tests.run_all
"""
from __future__ import annotations

import importlib
import inspect
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

# 自检模块（__main__ 里带断言）也一起跑，它们是纯标准库、秒级完成
SELF_CHECK_MODULES = (
    "app.pipeline.normalize",
    "app.pipeline.compare",
    "app.db.supabase",      # 无 SDK / 无密钥也必须能 import 并跑自检
    "app.db.rest",          # PostgREST 通道：无网络也要能验证查询构造正确
    "app.analytics",        # GBS ROI 公式：假设可改，公式不能漂
)

# pytest 风格测试模块
TEST_MODULES = (
    "tests.test_repo_contract",
    "tests.test_api",
    "tests.test_agents",            # 多 Agent 编排：四步全跑、短路、同源性
    "tests.test_stream",           # 实时拉取：游标轮转、只读、与提交产物一致
    "tests.test_dataset_regression",  # 全量 520 封 + 官方打分（最慢，放最后）
)


def run_self_checks() -> list[tuple[str, bool, str]]:
    """把自检模块当**子进程**跑，确保 `if __name__ == "__main__"` 里的断言真的执行。

    ★ 曾经踩过的坑：早先版本用 importlib 导入再找 `main()`，而这些模块的断言写在
      `__main__` 块里 —— 结果是全部报 PASS 却一行断言都没跑，典型的假绿。
    """
    import os
    import subprocess

    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT / "backend")}
    results: list[tuple[str, bool, str]] = []
    for name in SELF_CHECK_MODULES:
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [sys.executable, "-m", name], cwd=str(ROOT / "backend"), env=env,
                capture_output=True, text=True, timeout=300)
            ok = completed.returncode == 0
            tail = (completed.stdout or completed.stderr or "").strip().splitlines()
            detail = tail[-1][:110] if tail else f"exit={completed.returncode}"
            if not ok and completed.stderr:
                detail = completed.stderr.strip().splitlines()[-1][:110]
        except subprocess.TimeoutExpired:
            ok, detail = False, "超时 300s"
        except Exception as exc:           # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        results.append((name, ok, f"{detail}  ({time.perf_counter() - started:.2f}s)"))
    return results


def run_test_functions() -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    for name in TEST_MODULES:
        module = importlib.import_module(name)
        tests = [(key, obj) for key, obj in vars(module).items()
                 if key.startswith("test_") and inspect.isfunction(obj)]
        for key, func in sorted(tests):
            label = f"{name}::{key}"
            started = time.perf_counter()
            try:
                func()
                results.append((label, True, f"{time.perf_counter() - started:.2f}s"))
            except Exception as exc:       # noqa: BLE001
                results.append((label, False, f"{type(exc).__name__}: {exc}"))
                if "--verbose" in sys.argv:
                    traceback.print_exc()
    return results


def main() -> int:
    from app.console import harden_console

    harden_console()
    print("=" * 78)
    print("模块自检")
    print("=" * 78)
    results = run_self_checks()
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:34s} {detail}")

    print("\n" + "=" * 78)
    print("测试用例（含全量 520 封回归 + 官方打分）")
    print("=" * 78)
    case_results = run_test_functions()
    for name, ok, detail in case_results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:52s} {detail}")

    all_results = results + case_results
    failed = [name for name, ok, _ in all_results if not ok]
    print("\n" + "=" * 78)
    print(f"总计 {len(all_results)} 项：{len(all_results) - len(failed)} 通过，{len(failed)} 失败")
    for name in failed:
        print(f"  FAIL  {name}")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
