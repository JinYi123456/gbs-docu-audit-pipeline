# 架构与 AI 定位（答辩主线）

> 一句话：**我们没有"用 AI 跑一遍规则"，而是把 AI 放在它唯一不可替代的位置 ——
> 感知层（多模态读单），把可复现的裁决权交给确定性判定矩阵。**

这份文档回答评委一定会问的三件事，并给出**可现场演示的证据命令**。

---

## 1. 四层架构（谁负责什么，为什么这样切）

```
                        ┌──────────────────────────────────────────┐
   邮件 / 上传件  ────► │  Triage Agent      语义分流（闸门）        │  规则优先 + flash 兜底
                        └───────────────────┬──────────────────────┘
                                            ▼
                        ┌──────────────────────────────────────────┐
                        │  Extractor Agent   多模态感知（7 字段）    │  ★ AI 核心：PDF 原生字节
                        └───────────────────┬──────────────────────┘
                                            ▼
                        ┌──────────────────────────────────────────┐
                        │  Cross-Verifier    判定矩阵（确定性）      │  归一化 / 容差 / 证据
                        └───────────────────┬──────────────────────┘
                                            ▼
                        ┌──────────────────────────────────────────┐
                        │  Escalation Judge  升级裁决（Ask for help）│  4 类理由 + 人审工单
                        └──────────────────────────────────────────┘
                                            │
                        submission_view / dashboard / upload_runs（Supabase）
```

| 层 | 是否有概率性 | 为什么必须这样 |
|---|---|---|
| Triage | 规则 → LLM | 5 类分流占 Stage-1 30% 权重；闭集模板可逐字复核，不确定才付费给模型 |
| **Extractor** | **是（LLM）** | 这是 AI 唯一不可替代的位置：扫描件、多语言表头、表格版式，规则根本读不了 |
| **Cross-Verifier** | **否（确定）** | `defect_fields` 要求集合完全相等，概率采样会把 50% 的分数变成赌博 |
| Judge | 否（确定） | 升级策略是业务规则，不该有随机性；误升级 = 净亏损 |

**核心论点：感知层可以是概率的，裁决层必须是确定性的。**
这正是"AI 是核心功能"的诚实版本 —— AI 承担了规则做不到的那一半（读图），
而分数由可复现的那一半保证。

## 2. AI 到底做了什么（不是修辞，是可验证的事实）

三个 agent 的真实调用链，全部有轨迹留痕（`AgentStep`：耗时 / 模型 / 结论 / 证据）：

| 能力 | 模型（探活结果） | 形态 | 证据 |
|---|---|---|---|
| 邮件分类 | `gemini-3.1-flash-lite` | `response_schema=ClassifyOut` 强制 JSON | `python -m tools.live_smoke` |
| **多模态抽取** | `gemini-3.6-flash` | **PDF 原生字节直喂**（保留表格/两栏） | 同上，输出 7/7 字段 |
| 单档兜底 | 候选链自动降级 | 404/限额/503 自动换型号 | `.cache/gemini/model_plan.json` |

**实测记录（2026-09-19，真实 API Key）**：

```
[ OK ] AI · 邮件分类（强制 LLM 通道）
       email_059 → BL_COMPARISON（置信度 1.00，3420ms）  model=gemini-3.1-flash-lite
[ OK ] AI · 多模态字段提取（原生 PDF 字节）
       email_059 抽出 SI 7/7 字段（8471ms，mode=PAIR）    model=gemini-3.6-flash
       si_values: shipper="APRIL FINE PAPER TRADING ON BEHALF OF VITAL SOLUTIONS PTE LTD"
                  consignee="BALL & DOGGETT AUSTRALIA PTY LTD"  container_count=6
                  gross_weight_kg=131322.0
本次 AI 用量：{"calls": 2, "prompt_tokens": 6584, "output_tokens": 1104, "failures": 0}
```

上传路径的完整浏览器实测（前端 → FastAPI → Gemini → 判定矩阵 → 红框）：

```
POST /api/verify → 200
结论：MISMATCH · 缺陷 2 个：container_count, gross_weight_kg
抽取：LLM gemini-3.6-flash · PAIR · 16789ms
红框理由：集装箱数量：箱数相差 2 个（零容差）／毛重相差 2000.000 KG，超出容差
```

## 2.5 现场可见的三块「真数据」面板（本轮新增）

这三块都是为了回答同一个问题：**"怎么证明这些是算出来的，不是画出来的？"**

> 界面已全量英文化；下表面板名为界面上的实际文案（纯英文交付要求）。

| 面板（界面文案） | 数据来源 | 为什么不是演示动画 |
|---|---|---|
| **⚡ Fetch Latest GBS Mail**（顶部） | `POST /api/stream/pull` → `app/stream.py` | 点一次后端真跑一批（候选池 126 对、环状游标、并发 4）；卡片上的 email_id / 缺陷字段 / 耗时全部来自那次真实返回。前端只加了"错峰翻牌"的呈现节奏（确定性通道 <1s 就全跑完，逐封翻牌是给人看的节奏） |
| **Automation ROI** 三张大卡 | `app/analytics.py::compute_roi` | 输入就是本批产物（逐封 `decided_by`、真实耗时、126 对）；所有"假设"（20min/对、$65/人时、月 400 对、token 单价）在 **Basis & Assumptions** 里逐条列出，可改 env 重算 |
| **AI Multi-Agent Decision Trace**（红框下方） | 快照 `trace` 或 `/api/agents/trace/{id}` 现算 | 每步 `duration_ms` 是实测、`evidence` 是真实产物（命中规则名、解析字段数、逐字段相似度/容差、缺陷集合）；**Re-run live** 会重跑编排器并回传 `matches_submission` |

一条硬约束贯穿三块：**轨迹的 `agent` 用机器 id（`triage`/`extractor`/`cross_verifier`/`escalation_judge`），
与真实编排器 `Agent.name` 逐字一致，展示名在前端按 `role` 映射** ——
否则"快照轨迹"和"现算轨迹"就得写两个渲染器，这种不一致迟早会露出马脚。

另一条：`/api/stream/pull` **只读**。它绝不写 `emails` / `submission_view`，
`tests/test_stream.py::test_pull_agrees_with_official_submission` 把"拉取结论必须与提交产物一致"钉死，
`tests/test_api.py::test_stream_pull_rotates_and_never_touches_submission` 把"点一百次也不能动 520 键"钉死。

---

## 3. 三个必须能当场回答的追问

**Q：规则引擎拿了 1.0000，还要 AI 干什么？**
A：规则引擎能满分，是因为**官方语料的 126 份对照件恰好是结构化文本/Excel**。
真实业务里客户发来的是扫描件、手机拍的照片、带手写批注的 PDF —— 规则读不了。
把 Extractor 换成多模态后，同一套判定矩阵能处理这些输入（上传面板就是演示这个）。
规则通道保留为**降级路径**而不是主路径：它保证断网/限额/模型下线时链路仍然可用。
→ 现场演示：上传面板关掉"用 Gemini 多模态"再跑一次，结果同源，只有置信度口径不同。

**Q：怎么保证 AI 不会把分数搞坏？**
A：AI 的输出**不能直接成为结论**。它只产出 7 个字段 + 置信度 + 原文证据，
然后经过：① 本地 Schema 校验（默认值护栏、类型强制、幻觉键剔除）
② 归一化（实体去噪、港口拆码、单位换算）
③ 确定性容差判定（文本相似度 ≥0.94、重量 ±10kg 或 0.2%）
④ 升级阶梯（低置信度 → 人审，而不是猜）。
分数由 ②③④ 决定，三者零随机性。

**Q：你说 AI 是核心，代码在哪？**
A：`backend/app/agents/`（四个 Agent + 编排器）、`backend/app/llm/`（Schema 护栏、
指数退避、内容寻址缓存、模型探活）、`backend/app/pipeline/extract.py`（原生多模态通道）。
一条命令看全部轨迹：`cd backend && python -m app.agents.orchestrator --email email_031`。
也直接打开工作台右侧的「AI Multi-Agent 决策轨迹」面板：
点「现算一次」会真的重跑编排器，并回传 `matches_submission`。

**Q：轨迹面板里的"思考日志"是不是为了好看编的？**
A：当场验证：① 点「现算一次」→ 面板显示"与提交产物一致 ✓"（结论逐字段对账）；
② 点任一步展开 `evidence`，里面是这一步真实的输入输出
（命中规则名、解析到几个字段、每个字段的相似度/Δ、缺陷集合）；
③ 换成 `python -m app.agents.orchestrator --email email_031` 命令行看同一份轨迹。
快照轨迹的耗时是导出时对每个阶段**原地计时**的，与红框同一次计算。

**Q：ROI 大卡上的钱是怎么算的？**
A：全部来自 `analytics.compute_roi`，输入是本批真实产物；
点「口径与假设」就能看到 6 项假设与 5 条推导来源（`provenance`）。
改 `.env` 里的 `ROI_HUMAN_MINUTES_PER_PAIR` / `ROI_USD_PER_HUMAN_HOUR` / `ROI_MONTHLY_PAIRS`
重跑一次，数字就变 —— 能现场重算的估算才是可信的估算。
年化刻意不做"假设每天 10 万封"这类拍脑袋：而是拿本批**每对成本**外推到设定的月处理量，
倍数（`batch_scale`）也明示在卡上。

## 4. History：这一层是怎么被真实数据"打"出来的

工程叙事比 PPT 更有说服力，这几条都有测试/日志可查：

1. **模型下线**。`.env` 里写死的 `gemini-2.5-pro/flash` 对新用户返回 404；
   免费档对 pro 的输入配额是 `limit: 0`（429 必然发生）。→ 改为**探活候选链**：
   启动时用小请求逐个测活、结果落盘缓存 24h、调用时失败自动换型号。
2. **提示词文件缺失**。`classify.system.md` / `extract.system.md` 从未落盘，
   LLM 通道每次都在 `FileNotFoundError` 后被静默降级 —— "AI 接好了"是假象。
   → 补齐提示词 + `tools/live_smoke` 实弹烟测（这是唯一能戳破假象的手段）。
3. **Schema 默认值陷阱**。`google-genai` 拒绝任何带 `default` 的 Pydantic Schema
   （`ValueError: Default value is not supported`），且**只在请求时**才炸。
   → `assert_schema_is_gemini_safe()` 在启动期拦死，全部字段改为"必填但可空"。
4. **思考 token 吃预算**。2.5-pro 的 thinking token 计入 `max_output_tokens`，
   给小了 JSON 会被截断。→ 默认 8192，并把 `thinking_budget=0` 作为抽取档默认值。
5. **静默写失败**。httpx 不会对 4xx 自动抛异常，导致约束拒绝被当成写成功。
   → 显式检查状态码（实测：故意插入违反约束的行，修复前"写入成功"，修复后 23514 报错）。
6. **两处"看起来能用"的命名不一致**（本轮重新暴露）：
   导出轨迹里我写了 `TriageAgent` 这类类名，而编排器的 `Agent.name` 是 `triage`；
   判定层的证据键我猜的是 `defects/matched`，真实是 `defect_fields/matched_fields`。
   两者都会**静默通过**（前端渲染时不报错，只是少显示几个字段），
   靠测试把两份来源的 id 与键名对齐后才被发现。
   → 结论：跨模块的结构体要么共用一个构造器，要么用断言钉住键集。
7. **"单侧空白"的两次翻转**。先按直觉判成缺陷（GT 说 email_313/351 是 MISMATCH），
   一跑全量才发现 53 处单侧空白里只有 2 处是 gold 缺陷 → 改成**旁证升级**：
   孤立空白算不确定，只有同封已有确证缺陷时才一并升级。
   这条规则同时把 `wrong_doc_type` 从 0/5 救回 5/5。
   （这条已在上轮完成，此处保留是因为它是"用数据推翻自己设计"的最佳例证。）
