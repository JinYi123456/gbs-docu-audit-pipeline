# 合规、配置与四人工分（提交前必读）

---

## 一、环境变量槽位（`.env` 在仓库根目录，永不入库）

> 已验证：**全部 19 个键都能被进程看见**（`python -m app.env` 只打印键名与长度，绝不回显值）。
> ⚠️ 一个曾经的致命坑：代码里没有任何地方加载 `.env`，所有配置都读 `os.environ`。
> 结果是你把密钥写进 `.env` 后，runner 依然认为"没有 API Key"，**静默走规则通道** ——
> AI 通道看起来接好了，其实从未被调用。现在 `app/__init__.py` 在任何子模块导入前加载
> `.env`，且**永不覆盖**平台注入的变量（Vercel/Railway 语义）。

| 槽位 | 用途 | 必填 | 当前状态 | 说明 |
|---|---|---|---|---|
| `GEMINI_API_KEY` | AI 通道凭证 | ✅ | set | 也可用 `GOOGLE_API_KEY` |
| `GEMINI_MODEL_CLASSIFY` | 分类档首选 | ✅ | `gemini-3.1-flash-lite` | 2026-09 实测可用 |
| `GEMINI_MODEL_EXTRACT` | 抽取档首选 | ✅ | `gemini-3.6-flash` | 多模态 + 结构化输出实测可用 |
| `GEMINI_MODEL_FALLBACK` | 降级型号 | ⬜ | `gemini-3.5-flash` | 候选链里的第二顺位 |
| `GEMINI_CLASSIFY_THINKING` | 分类思考预算 | ⬜ | `0` | flash 可关思考：更快更省 |
| `GEMINI_EXTRACT_THINKING` | 抽取思考预算 | ⬜ | `0` | 思考 token 计入输出预算，关掉可免截断 |
| `GEMINI_TIMEOUT_S` | **单次调用硬超时** | ⬜ | `120` | 防止慢请求占满并发名额拖死整批 |
| `GEMINI_MAX_OUTPUT_TOKENS` | 输出上限 | ⬜ | `8192` | 给小了 JSON 会被思考 token 挤断 |
| `GEMINI_CONCURRENCY` | 并发上限 | ⬜ | `8` | 与 `PIPELINE_CONCURRENCY` 独立 |
| `GEMINI_CACHE` / `GEMINI_CACHE_DIR` | 内容寻址缓存 | ⬜ | `1` / `.cache/gemini` | 改提示词重跑 520 封几乎零成本 |
| `SUPABASE_URL` | 云端地址 | ✅ | set | PostgREST 端点 |
| `SUPABASE_SERVICE_ROLE_KEY` | service-role | ✅ | set | **只在后端进程内，绝不进前端 bundle** |
| `INBOX_SOURCE` / `INBOX_DATA_DIR` | 数据源 | ⬜ | `data` / `data` | 或 `http://localhost:8080` |
| `SUBMIT_URL` | 官方评分服务 | ⬜ | `http://localhost:8080` | Docker compose |
| `GROUND_TRUTH_PATH` / `SCORING_PATH` | 离线自评 | ⬜ | `eval/...` / `server/...` | 仅本地用 |

### 还没写进 `.env` 的可选槽位（需要时再加）

| 槽位 | 默认 | 何时需要 |
|---|---|---|
| `NEXT_PUBLIC_API_BASE` | `http://localhost:8000` | 前端指向远端 FastAPI（Vercel → Railway） |
| `CORS_ORIGINS` | `http://localhost:3000` | 云端前端域名（逗号分隔） |
| `SUPABASE_TIMEOUT_S` / `SUPABASE_MAX_ATTEMPTS` | `30` / `4` | 云端抖动时调整写入重试预算 |
| `GEMINI_CLASSIFY_CANDIDATES` / `GEMINI_EXTRACT_CANDIDATES` | 内置链 | 手动指定候选型号顺序（逗号分隔） |
| `GEMINI_MODEL_PROBE` | `1` | 置 `0` 可跳过启动探活（离线/省 token） |
| `API_PORT` | `8000` | 换端口 |
| `MANUAL_OVERRIDES_PATH` | `eval/report/manual_overrides.json` | 断网时人工改判的落盘位置 |
| `ROI_HUMAN_MINUTES_PER_PAIR` | `20` | 答辩现场被质疑人工时长时，当场改当场重算 |
| `ROI_USD_PER_HUMAN_HOUR` | `65` | 换成客户自己的 GBS 人力成本口径 |
| `ROI_MONTHLY_PAIRS` | `400` | 年化外推的月处理量基准（卡上会显示外推倍数） |
| `ROI_USD_PER_1M_INPUT` / `ROI_USD_PER_1M_OUTPUT` | `0.30` / `2.50` | 换成客户实际拿到的 token 单价 |

---

## 二、一键命令（`make` 不可用时看右列）

| 目的 | make | 等价命令 |
|---|---|---|
| 解包官方数据 | `make bootstrap` | `bash scripts/bootstrap.sh` |
| 数据体检 | `make verify` | `python3 scripts/verify_dataset.py` |
| **AI + 云实弹烟测** | — | `cd backend && python -m tools.live_smoke` |
| 全量跑批（不花钱） | — | `cd backend && python -m app.runner --no-llm` |
| 全量跑批 + 落云 | `make run` | `cd backend && python -m app.runner --no-llm --write-db` |
| 多 Agent 轨迹复盘 | — | `cd backend && python -m app.agents.orchestrator --email email_031` |
| **上传即核对（命令行）** | — | `cd backend && python -m app.upload <SI> <BL>` |
| **现算一封的多 Agent 轨迹** | — | `curl localhost:8000/api/agents/trace/email_031` |
| **实时拉取一批（只读）** | — | `curl -X POST 'localhost:8000/api/stream/pull?batch_size=6'` |
| 起后端 | `make api` | `cd backend && python -m uvicorn app.main:app --port 8000` |
| 起前端 | `make web` | `cd frontend && npm run dev` |
| 全量测试（51 项） | `make test` | `cd backend && python -m tests.run_all` |
| 官方自评（云端取数） | — | `cd backend && python -m tests.test_submit --source db --offline` |
| 推 GitHub 前护栏 | `make check-leaks` | `git ls-files \| grep -Ei 'ground_truth\|\.zip$\|\.env$'` |

---

## 三、云基础设施现状（已实测，不是"计划中"）

| 项 | 状态 | 证据 |
|---|---|---|
| Supabase 项目 | 已连通 | `RestGateway` 读写往返验证通过 |
| 迁移 0001–0005 | **已应用到云端** | `emails`/`extracted_fields`/`comparisons`/`review_queue`/`upload_runs` + 3 视图 + 3 RPC |
| 520 封落库 | ✅ 520 行父行 | `emails=520 · comparisons=868 · review_queue=51` |
| `submission_view` | ✅ 恰好 520 键 | 官方打分器读它得到 **1.0000** |
| Realtime | ✅ 3 张表已加入 publication | `emails`/`review_queue`/`comparisons`（+`upload_runs`） |
| RLS | ✅ 匿名只读 + 改判走 RPC | `apply_manual_verdict` / `revert_manual_verdict` / `mark_submitted` |
| 无 SDK 环境 | ✅ httpx 直连 PostgREST | 本机从未安装 `supabase`，落库照常工作 |

**关键隔离**：上传件只进 `upload_runs`，**绝不进 `emails`**。
`submission_view` 是从 `emails` 聚合的 520 键集合，多一个未知键就被官方记罚 ——
现场随手点一次上传不会打掉满分。这条有专门的回归测试（`test_upload_never_pollutes_official_submission`）。

### 公有云部署遗漏项清单（Vercel + Railway）

**Railway（后端）**
1. 启动命令要显式 `--host 0.0.0.0`：`uvicorn app.main:app --host 0.0.0.0 --port $PORT`（否则只绑 127.0.0.1，健康检查失败）。
2. 依赖里 `supabase` 是**可选**的（我们走 REST 通道），但 `httpx`、`fastapi`、`uvicorn[standard]`、`python-multipart` 必须装 —— 缺 `python-multipart` 会让 `/api/verify` 直接 500。
3. 变量注入顺序：平台变量优先于 `.env`（我们的加载器不覆盖已存在变量，符合该语义）。
4. `data/` 与 `eval/` 是 bootstrap 产物，**不在镜像里**。容器内要么跑 `scripts/bootstrap.sh`，要么挂持久卷；`--write-db` 的跑批需要在容器内能读到附件。
5. 冷启动时模型探活会跑一次真实小请求（约 2s）；不想付就设 `GEMINI_MODEL_PROBE=0` 并提交 `model_plan.json`。

**Vercel（前端）**
6. `NEXT_PUBLIC_API_BASE` 必须指向 Railway 的公网域名，**并且**后端 `CORS_ORIGINS` 要包含 Vercel 域名（默认只放 `http://localhost:3000`，跨域会被拦）。
7. `frontend/lib/data.ts` 读的是仓库相对路径的 `dashboard.json`。Vercel 上**没有这个文件**（它被 `.gitignore` 排除）→ 页面会显示引导页而不是 500（这是刻意设计）。要在线展示快照，就把 `eval/report/dashboard.json` 作为构建产物提交或用 `DASHBOARD_PATH` 指向挂载路径。
8. 上传功能依赖后端，Vercel 上的前端必须能访问 Railway 域名；纯静态预览下"上传即核对"会显示可读的离线提示（含启动命令），不会白屏。
9. 本轮新增的「⚡ 实时拉取」与「现算轨迹」也依赖后端（前者跑 126 对的候选池 + 并发 4，后者现跑一次编排器）。纯快照模式下两者都会给出可执行提示；**轨迹面板在无后端时仍可读快照轨迹**，不会变成空面板。
10. 实时拉取是 `POST`，Vercel → Railway 的跨域预检要放行 `POST` 与 `Content-Type`（现有 `allow_methods=["*"]` 已覆盖）。

---

## 四、四人分工（截止 9/22 12:00，按"卡点"排序）

> 原则：每个人都有一个**阻塞别人**的卡点动作，先解卡再做增量。

| 角色 | 卡点动作（必须先做完） | 增量目标 | 验收 |
|---|---|---|---|
| **A 分类/编排** | 用 `tools.live_smoke` 确认 AI 通道在**你的机器上**可用（看 `resolved` 型号 + `calls>0`） | 把 `--agents` 接进跑批（编排器已就绪，需在 runner 里开一个 flag）；补 3 个真实"格式刁钻"的扫描件的分类用例 | `python -m tools.live_smoke` 全绿；`agents` 轨迹出现在结果里 |
| **B 入料/抽取** | 提交一份**真实 PDF**（非语料）走通后端 `/api/verify` 并核对 7 字段 | 提高多模态抽取的字段级准确率（提示词 v4 + 证据字段）；补图片型扫描件的降级路径 | 上传面板上 PDF 能出 7/7 字段；`extractor.error == null` |
| **C 比对/评测** | 跑 `python -m tests.test_submit --source db --offline`，确认云端取数仍是 1.0000 | 为判定矩阵补边界用例（容差临界值、别名、港口拆码）；把容差参数写进配置而不是硬编码 | `make test` 51/51；新增用例覆盖临界值 |
| **D 数据/前端** | 部署 Vercel 预览并配 `NEXT_PUBLIC_API_BASE` + 后端 `CORS_ORIGINS` | 工作台接 Realtime（订阅 `upload_runs`/`review_queue`，上传后自动刷新"最近核对"）；把 AI 状态页做成可视化卡片 | 打开线上域名能看快照 + 上传能出红框 |

**共同的提交前 5 分钟流程**（写在 README 顶部也行）：
```bash
make check-leaks                     # 答案键 / zip / .env 绝不能被追踪
cd backend && python -m tests.run_all         # 51/51
cd backend && python -m tests.test_submit --source db --offline   # 1.0000
```

---

## 四点五、5 分钟 Demo 录像脚本（按这个顺序录，每段都有按钮可点）

> 前提：两个服务都在跑（`make api` + `make web`），浏览器开 http://localhost:3000。
> 关键原则：**每一句话都能当场点开验证** —— 不靠形容词。

| 时间 | 画面/动作 | 口播要点 |
|---|---|---|
> ⚠️ **UI 已全量英文化**（纯英文交付要求）。下表按钮名按界面实际英文文案标注，口播也建议用英文术语。

| 时间 | 画面/动作（按钮文案） | 口播要点 |
|---|---|---|
| 0:00–0:30 | 顶部标题 + `Official Grading: 1.0000` 徽标 | "520 real emails, 5 categories, 7 canonical fields, 46 defects — all five metrics of the official grader are maxed out. Every number below is reproducible." |
| 0:30–1:20 | 点 **⚡ Fetch Latest GBS Mail** | "This is not a pre-recorded animation: the backend picks the next batch from **126** SI/draft-BL pairs and runs the full pipeline in 0.2–0.8s, verdict by verdict. Note it is read-only — it can never damage the submitted payload."（再点一次说明游标前进、不重复） |
| 1:20–2:00 | 点某封的 **View diff** | "Seven fields side by side; the red boxes are the defect set. The official score requires an **exact set match**, so we are not allowed to over- or under-report." |
| 2:00–3:00 | 展开 **AI Multi-Agent Decision Trace** + 点 **Re-run live** | "Four stages: routing → multimodal extraction → deterministic judgement → escalation. 'Re-run live' actually re-executes the orchestrator and then reports 'Consistent with submission ✓'. Expand any step for its evidence: which rule matched, each similarity score, the tolerances." |
| 3:00–3:40 | 展开 **Basis & Assumptions** | "All assumptions are on the table: 20 minutes per pair, $65 per hour, 400 pairs a month. Change the env and re-run — an estimate you can recompute live is the only estimate worth trusting."（可选：把 ROI_HUMAN_MINUTES_PER_PAIR 改成 25 再刷新） |
| 3:40–4:30 | 点 **On-Demand Audit (Real-time)** → **Load sample SI** / **Load sample BL** → **Run audit** | "Real customers send scans and annotated PDFs that rules cannot read. Here the native PDF bytes go straight to the model and the same judgement matrix produces the verdict; switch off 'Gemini multimodal' and re-run — same source of truth, just a degraded channel." |
| 4:30–5:00 | Human-in-the-Loop Override：勾字段 → **Confirm mismatch** / **Override to matched** | "Human review is the fourth stage, not a band-aid: the override lands through the `apply_manual_verdict()` RPC, `submission_view` reflects it, and the payload preview on the right updates live — no scoring code changes." |

**录像前 30 秒自查**：`curl -s localhost:8000/health`（看 `source` 与 `supabase`）、
`curl -s -X POST 'localhost:8000/api/stream/pull?batch_size=3&reset=true'`（看是否返回 items，
避免录像中首次点按钮才冷启动）。

---

## 五、网络与超时加固（本轮补齐，都有代码位置）

| 风险 | 症状 | 措施 |
|---|---|---|
| Gemini 慢/挂 | 单封卡住 → 并发名额占满 → 整批停摆 | `asyncio.wait_for` 硬超时（`GEMINI_TIMEOUT_S=120`），超时按瞬时错误退避重试 |
| 模型下线/配额为 0 | 每封白等 5 次退避重试（约 30s × 520） | 探活把"型号不可用"判为**永久错误**→ 立即换链上下一个型号；候选链剔除探活失败的型号 |
| 云端写入风暴 | 每行占一个线程等超时，500+ 行批量写放大 | 分块 100 行 + 指数退避抖动 + `arun` 外层硬超时（预算 = 单次超时 × 尝试次数 + 退避） |
| SDK 缺失/版本差异 | 落库静默跳过，云端永远空 | httpx 直连 PostgREST 的降级通道；`timeout_s` 通过探测式 `ClientOptions` 真正生效 |
| 前端请求挂死 | 按钮转圈到浏览器 300s 超时 | `AbortController`：健康 8s / 上传 180s，超时给可读提示而不是白屏 |
| 实时拉取被拖死 | 有人把 `use_llm=true` 打开，一批 6 封走多模态 | 前端 45s 超时 + 后端 `batch_size` 钳位到 24；默认 `use_llm=false`（确定性通道实测 0.2–0.8s/批） |
| 现场反复点拉取 | 游标乱跳、重复展示同一批 | 后端环状游标（`StreamCursor`）+ 前端每次取下一批；`reset=true` 才回到开头 |
| 单封失败 | 一封坏文件毁掉整批 520 或整批拉取 | 每封 try/except（拉取链路同样隔离，有测试钉住）+ 失败也写父行（保证视图不缺键） |
| 演示把满分打坏 | 现场点一次上传/拉取就污染 520 键 | 上传只进 `upload_runs`；拉取**只读**；两条都有回归测试钉死 |
