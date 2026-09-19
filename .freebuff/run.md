# 运行手册

本文件是 **Preview 标签页的依据**，也是新队友上车的第一份清单。两条主线：
**后端跑批**（Python → 产出 `submission.json` / `dashboard.json`）与**前端工作台**（Next.js）。

关键设计：**工作台的只读部分不依赖后端进程、不需要任何密钥**。它只读一个静态快照文件，
所以断网、没起 FastAPI、没配 Supabase 时，工作台照样能开——演示环境里这条极其重要。

**新增的实时部分**：工作台右上角的「上传即核对」面板会调用本机 FastAPI 的 `/api/verify`，
跑完 分类 → 多模态抽取 → 判定矩阵 → 升级裁决 全链路并回显红框。
它需要后端在跑（缺后端时面板会给出可读的启动指引，不会白屏）。

---

## 1. 复现产物（PROCEDURE，不含任何密钥）

```bash
# ① 一次性：解包官方两个 zip
make bootstrap        # Windows: powershell -File scripts/bootstrap.ps1
```

`make bootstrap` 做四件事：

| 动作 | 结果 | 是否入库 |
|---|---|---|
| 官方参赛包 → `data/` | 520 封邮件 + 250 个附件 + `loader.py` + `sample_submission.json` | 否（`.gitignore` 排除） |
| 官方评分服务 → `server/` | `scoring.py` / `app.py` / `Dockerfile` | 否 |
| 答案键单独解到 `eval/private/` | `ground_truth.json`（**红线，绝不入库**） | 否 |
| 校验数据集完整性 | 520 封 / 250 附件 / 46 封缺陷 | — |

```bash
# ② 跑全量 520 封（确定性通道，零依赖、零 API Key）+ 导出前端快照
cd backend
python -m tests.test_dataset_regression --with-diagnostics \
    --dashboard ../eval/report/dashboard.json
```

产物：

- `eval/report/submission.json` —— 官方 5 键提交产物（520 条）
- `eval/report/dashboard.json` —— 前端工作台读的快照（邮件元信息 + 7 字段比对明细）
- `eval/report/best.json` —— 历史最高分快照（反回归护栏）
- `eval/report/run_<ts>_<tag>/` —— 每次跑批的完整留档

```bash
# ③ 自评测：本地打分（Docker 没起也能跑）或打给官方服务
python -m tests.test_submit --source file --offline          # 本地 scoring.py
python -m tests.test_submit --source file                    # POST 到官方 /submit
python -m tests.test_submit --source db --with-diagnostics   # 走 Supabase 视图（人工改判已生效）
```

---

## 2. 起前端 dev server

```bash
cd frontend
npm install          # 首次
npm run dev          # → http://localhost:3000
```

- 端口：**3000**（`package.json` 的 `dev` 脚本固定 `-p 3000`；被占用就改成别的并同步本文档）
- 数据来源：`../eval/report/dashboard.json`（可用环境变量 `DASHBOARD_PATH` 覆盖，
  官方分数徽章读 `../eval/report/best.json`，可用 `BEST_SCORE_PATH` 覆盖）
- 没有快照时页面不会 500，而是给出生成快照的三条命令（见 `app/page.tsx`）

Windows 后台起服务（脱离本会话，日志落到 `.freebuff/`）：

```bash
powershell -NoProfile -Command "(Start-Process -FilePath 'npm.cmd' -ArgumentList 'run','dev' \
  -RedirectStandardOutput '.freebuff\preview.log' \
  -RedirectStandardError '.freebuff\preview.log.err' -WindowStyle Hidden -PassThru).Id"
```

注意：`Start-Process` 必须写可执行文件全名（`npm.cmd`，不是 `npm`），且 stdout/stderr
必须指向**两个不同文件**；命令会因等待重定向句柄而在 60s 后超时，但服务本身已经起来了
（用 `netstat -ano | grep :3000` 确认 pid）。

---

## 3. 后端 API（可选）

前端工作台**不依赖**它（只读快照），但它提供实时数据源与人工改判写入口：

```bash
make api        # → http://localhost:8000/docs
```

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 健康度（只回 `set`/`missing`，绝不回显密钥） |
| GET | `/api/summary` | KPI 汇总 |
| GET | `/api/emails` | 列表（`status` / `category` / `only_defect` / `q` / 分页） |
| GET | `/api/emails/{id}` | 单封详情（含 7 字段逐项比对） |
| GET | `/api/review-queue` | 待人工处理（按优先级排序） |
| POST | `/api/review` | 人工改判 → Supabase RPC；云端不可用则写本地改判文件 |
| DELETE | `/api/review/{id}` | 撤销改判，回到机器裁决 |
| GET | `/api/submission` | 官方 5 键产物（人工改判已生效） |
| POST | `/api/submit` | 转投官方评分服务并回传分数 |
| GET | `/api/agents` | 多 Agent 花名册（机器 id + role + 职责） |
| GET | `/api/agents/trace/{id}` | **现算**一封的多 Agent 轨迹 + `matches_submission` 自证 |
| POST | `/api/stream/pull` | 「⚡ 实时拉取」下一批（环状游标，**只读**，不回写提交产物） |

数据源**云端优先、本地兜底**：配了 Supabase 就读 `submission_view` / `pipeline_stats`，
没配就读跑批快照 + `eval/report/manual_overrides.json`。

---

## 3.5 AI 通道与云基础设施（本轮新增，都已实测）

```bash
# ① 实弹烟测：真调 Gemini（分类 + 多模态抽取）+ 真探 Supabase 表
#    会打印每个候选型号的探活结果、真实 token 用量、以及表是否可读
cd backend && python -m tools.live_smoke

# ② 多 Agent 轨迹复盘（看四段计算各自做了什么、用了哪个模型）
cd backend && python -m app.agents.orchestrator --describe
cd backend && python -m app.agents.orchestrator --email email_031

# ③ 上传即核对（命令行版，走与前端完全相同的链路）
cd backend && python -m app.upload ../data/attachments/email_031_SI.txt ../data/attachments/email_031_BL.txt

# ④ 全量跑批 + 写入云端（无 SDK 也可写：自动走 httpx 直连 PostgREST）
cd backend && python -m app.runner --source data --no-llm --write-db

# ⑤ 从**云端视图**取数并打分（证明云链路与本地逐字一致）
cd backend && python -m tests.test_submit --source db --offline
```

**已实测的关键结论**：`--source db --offline` 得到 **1.0000**
（Stage-1 1.0 / defect-F1 1.0 / E2E 46/46 / 升级召回 20/20），
云端 `emails` 520 行、`submission_view` 恰好 520 键。

⚠️ 两个已修、别改回去的陷阱：
1. `.env` 必须由 `app/__init__.py` 在**任何子模块导入前**加载，否则密钥不可见，
   AI 通道会静默降级为规则通道（看起来一切正常，其实一次 API 都没调）。
2. `gemini-2.5-*` 已对新用户下线（404）；免费档对 pro 配额为 0（429）。
   型号一律通过候选链探活解析，不要写死。

---

## 4. 可选：数据库

```bash
# Supabase 建表（5 张表 + 3 视图 + Realtime + RLS + 3 个 RPC）
#   推荐用 Supabase MCP / CLI 应用；手工则在 SQL Editor 里按顺序执行：
#   backend/db/migrations/0001_enums.sql
#   backend/db/migrations/0002_tables.sql
#   backend/db/migrations/0003_views.sql
#   backend/db/migrations/0004_realtime_rls.sql
#   backend/db/migrations/0005_upload_runs.sql   ← 上传留痕（与 520 评测数据物理隔离）
cp .env.example .env         # 填 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / GEMINI_API_KEY

cd backend
python -m app.runner --source data --write-db     # 跑批 + 落库（幂等）
python -m app.db.supabase                         # 打印连接健康度（不回显密钥）
```

没有 Supabase 凭证时 `--write-db` 会**优雅跳过**并继续产出 submission.json，
这是刻意设计的：本地空跑绝不应被云依赖卡住。

---

## 4.5 现场演示的三个新面板（本轮新增）

> 界面已**全量英文化**（纯英文交付）。下表按钮名按界面实际英文文案标注。

| 面板 | 位置 | 一句话讲法 |
|---|---|---|
| **⚡ Fetch Latest GBS Mail** | 顶部深色条 | 点一次 → 后端真跑一批（126 对池、环状轮转），逐封从 PROCESSING 翻成 MATCHED/MISMATCHED；**只读**，动不了 520 键提交产物 |
| Automation ROI（三张大卡） | 顶部 | Token Cost Saved / Time Saved / Compliance Defects Caught，全部来自本批实测（`analytics.compute_roi`），假设与推导来源在「Basis & Assumptions」里可现场质疑 |
| **AI Multi-Agent Decision Trace** | 右侧详情、7 字段红框下方 | 折叠式思考日志：4 步 + 实测耗时 + 证据（点任一步展开 JSON）；「Re-run live」会真跑编排器并给出 "Consistent with submission ✓" |

演示前建议顺序：先 Refresh 工作台（快照）→ 点 ⚡ Fetch Latest GBS Mail → 点某封「View diff」
→ 展开轨迹并发起「Re-run live」→ 展开「Basis & Assumptions」。

⚠️「实时拉取」与「现算轨迹」需要后端在跑（第 3 节）；不跑也能看快照里的轨迹，
面板会给出可执行提示而不是转圈。

---

## 5. 验证清单

```bash
make test        # 一把跑完：4 个模块自检 + 47 个用例（共 51 项）
```

等价于 `cd backend && python -m tests.run_all`，它覆盖：

| 命令 | 检查什么 |
|---|---|
| `python -m app.pipeline.normalize` | 归一层（实体/港口/箱数/毛重） |
| `python -m app.pipeline.compare` | 判定矩阵（含陈旧 UN/LOCODE、嵌套实体、单侧空白旁证升级） |
| `python -m app.db.supabase` | 无 SDK / 无密钥时必须仍可 import 且健康检查可跑 |
| `python -m tests.test_repo_contract` | 仓储层离线契约（GENERATED 列、on_conflict、5 键自洽） |
| `python -m app.analytics` | GBS ROI 公式（人工时/时薪/年化，含空批量除零护栏） |
| `python -m tests.test_api` | API 契约（人工改判写回、非法输入 422、官方服务不可达 503、ROI 卡、Agent 轨迹、实时拉取只读） |
| `python -m tests.test_agents` | 多 Agent 编排（四步全跑、非对照类短路、轨迹证据形状） |
| `python -m tests.test_stream` | 实时拉取（游标轮转、批量钳位、单封失败隔离、与提交产物一致） |
| `python -m tests.test_dataset_regression` | 全量 520 封 + 官方打分（验收线 macro_f1≥0.90 / e2e≥0.80 / defect_f1≥0.90） |

注意：`tests.run_all` 把自检模块当**子进程**跑，因为它们的断言写在 `if __name__ == "__main__"` 里
（早先用 importlib 导入再找 `main()`，结果是全部报 PASS 却一行断言都没执行）。

`tests/test_submit.py` 会拿本次分数与 `best.json` 比对，**低于峰值就提示不要提交这一版**。
