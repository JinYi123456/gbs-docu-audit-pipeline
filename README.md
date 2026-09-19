# SDOC — Shipping Document Compliance Console (SI vs BL)

**Averis × Monash Hackathon 2026 · Global Business Services track**

An end-to-end auditing pipeline for ocean-freight documentation: it ingests the shared GBS mailbox,
routes every email by intent, extracts the 7 canonical fields from both the **Shipping Instruction**
(SI) and the **draft Bill of Lading** (BL), decides compliance with a **deterministic** comparison
matrix, escalates only where evidence is genuinely insufficient, and closes the loop with a human
review console backed by Supabase.

> **Design principle: perception may be probabilistic, the verdict must be reproducible.**
> A multimodal model is allowed to *read* a document. It is never allowed to *decide* the verdict —
> the graded output is produced by an explicitly-toleranced matrix that returns the same answer on
> every replay.

---

## 1. Project Overview & GBS Pain Points

### The operational reality

A shared-services documentation desk receives a constant inbound stream of customer and forwarder
mail — SI requests, draft BL amendments, invoice queries, operations notices, and a steady trickle of
phishing. Hidden inside that stream are the pairs that actually carry commercial and compliance risk:
the **SI against the draft BL**, where a wrong consignee, a stale UN/LOCODE, one container short or a
transposed digit on gross weight travels all the way into the bill of lading, the LC presentation and
the customs declaration.

Today that check is done by eye, field by field:

| Pain point | Operational consequence |
|---|---|
| Mailbox overload & mixed intent | Auditors triage by hand; real amendments queue behind routine traffic |
| Field-by-field eyeball comparison | ~**20 minutes per SI/BL pair**, fully non-scalable with volume |
| Scanned / annotated / mixed-format attachments | Rules-based scrapers fail outright; documents bounce back to the customer |
| No auditable trail per decision | Disputes cannot be reconstructed; "who checked what, and on what evidence?" is unanswerable |
| Defects escape to the carrier/customs | Re-issuance costs, demurrage, LC discrepancies — all avoidable |

### What SDOC does

1. **Routes** every inbound email into a closed set of 5 intents (fast expert rules first, model fallback only when rules are inconclusive).
2. **Extracts** the 7 canonical fields (`shipper`, `consignee`, `notify_party`, `port_of_loading`, `port_of_discharge`, `container_count`, `gross_weight_kg`) from both documents — PDFs are fed to Gemini as **native byte streams** so layout survives, structured formats go through the text channel.
3. **Compares** each field through normalisation plus an explicit judgement matrix, and returns the exact set of mismatched fields.
4. **Escalates** with a reason when the evidence cannot support a verdict (missing counterpart, unreadable scan, wrong document type, blank/placeholder values) instead of guessing.
5. **Closes the loop** in a review console: the auditor sees the red-boxed diff, the multi-agent reasoning trace, and can override the verdict — the override is persisted through a Postgres RPC and flows straight into the submission payload.

### Reference corpus (measured on the organiser dataset)

| Dimension | Value |
|---|---|
| Emails in the corpus | **520** |
| Attachments | 250 |
| `BL_COMPARISON` (SI vs draft BL) | 220 |
| Audited pairs (both sides readable) | **126** |
| Intent mix | 220 comparison · 125 SI request · 75 invoice query · 60 general · 40 spam |
| Outcomes | 46 mismatched · 51 escalated to human review · 423 clean |

---

## 2. Verified Results — Official Scoring

All figures below come from the **organiser's own scorer**, not from an in-house metric. The pipeline
is graded on the five-key submission contract (`category`, `status`, `review_reason`, `has_defect`,
`defect_fields`).

| Metric | Weight | Score |
|---|---|---|
| Stage-1 macro-F1 (5-class intent routing) | 0.30 | **1.0000** |
| Stage-3 defect-F1 (field-level) | 0.20 | **1.0000** |
| End-to-end exact defect-set match | 0.50 | **46/46 → 1.0000** |
| Escalation recall (diagnostic) | — | **20/20** — `missing_attachment` 5/5 · `missing_value` 5/5 · `unreadable` 5/5 · `wrong_doc_type` 5/5 |
| Field-level diagnostics | — | false negatives **0** · false positives **0** |
| **Final score** | | **1.0000** |

Reproduce it end-to-end (no API key, no cloud required — this is the deterministic path):

```bash
cd backend
python -m tests.test_submit --source file --offline   # organiser scorer, offline
```

The same run also drives the regression gates (`python -m tests.run_all` asserts floors on
macro-F1 ≥ 0.90, defect-F1 ≥ 0.90 and end-to-end ≥ 0.80 so a future change cannot silently degrade
the baseline).

---

## 3. Technology Integration & Architecture

### 3.1 Dual-Channel Fast-Track Router

The router is the reason a 520-email batch costs cents instead of dollars, without giving up
multimodal capability:

```
                   ┌──────────────────────────────────────────────────────────┐
   inbound mail ──▶ │  TriageAgent                                             │
                   │  ① closed-set expert rules  (verbatim reviewable)         │
                   │  ② Gemini flash tier + response_schema  (fallback only)   │
                   └───────────────┬──────────────────────────────────────────┘
                                   │ non-comparison intent → short-circuit (no extraction, no cost)
                                   ▼
                   ┌──────────────────────────────────────────────────────────┐
                   │  ExtractorAgent — multimodal perception                   │
                   │  PDF  → native byte stream (layout, multi-column, tables) │
                   │  other → text channel                                     │
                   │  failure → expert-rule extractor with an identical shape   │
                   └───────────────┬──────────────────────────────────────────┘
                                   ▼
                   ┌──────────────────────────────────────────────────────────┐
                   │  CrossVerifierAgent — DETERMINISTIC judgement matrix      │
                   │  normalise → exact / alias / fuzzy / numeric tolerance    │
                   │  text ≥ 0.94 · weight ±10 kg or 0.2% · containers zero-tol │
                   └───────────────┬──────────────────────────────────────────┘
                                   ▼
                   ┌──────────────────────────────────────────────────────────┐
                   │  EscalationJudgeAgent — escalation ladder                 │
                   │  confirmed mismatch ▸ attachment issue ▸ unreadable ▸      │
                   │  blank uncertainty        (never invents defect fields)   │
                   └───────────────┬──────────────────────────────────────────┘
                                   ▼
            5-key verdict per email ──▶ Supabase (Postgres + Realtime) ──▶ review console
```

### 3.2 The four agents

| Agent | Responsibility | Channel | Guarantee |
|---|---|---|---|
| **TriageAgent** | Route the email into one of 5 intents, decide whether it enters the comparison pipeline at all | Expert rules first; `gemini-flash` tier with a `response_schema` contract as fallback | Every rule hit is verbatim reviewable; non-comparison mail short-circuits before any extraction spend |
| **ExtractorAgent** | Read 7 canonical fields out of SI and BL | Gemini multimodal on raw PDF bytes; text channel otherwise; expert-rule extractor on failure | Identical output shape on every channel — the downstream chain cannot tell which one ran |
| **CrossVerifierAgent** | Decide, per field, matched / mismatched / undecided | **No model.** Pure functions + explicit tolerances | Zero randomness: the same input always yields the same verdict set |
| **EscalationJudgeAgent** | Decide whether to self-serve the verdict or hand it to a human | Deterministic escalation ladder | Side-channel hints (email body) shape the *reason* only; they can never create a defect field |

Every agent step is recorded as a structured `AgentStep` (duration, channel, model, evidence) — the
console renders it as a collapsible reasoning log, and the same trace is written to the cloud audit
trail for on-demand audits.

### 3.3 Cloud integration

**Google Gemini (`google-genai` official SDK)**

| Capability | Implementation |
|---|---|
| Structured routing output | `response_schema=ClassifyOut` — schema violations are rejected, not parsed leniently |
| Native multimodal extraction | PDF bytes are sent to the model directly (no OCR detour), so tables and two-column layouts survive |
| Hard timeout | `GEMINI_TIMEOUT_S` (default 120 s) via `asyncio.wait_for`, so one slow request cannot occupy a concurrency slot forever |
| Model self-healing | Startup probe over a candidate chain; 404 / quota-0 / 503 results are cached for 24 h and the next viable model is used automatically (hard-coded model IDs are a liability — two of ours were retired mid-project) |
| Cost control | Content-addressed cache (`GEMINI_CACHE_DIR`) — re-running 520 emails after a prompt change costs almost nothing |
| Verified live | 2 calls · 6,584 input + 1,104 output tokens · 0 failures: routing `email_059 → BL_COMPARISON` (confidence 1.00, 3,420 ms, `gemini-3.1-flash-lite`); multimodal extraction **7/7 SI fields in 8,471 ms** (`gemini-3.6-flash`) |

**Supabase (Postgres + Realtime)**

| Layer | Objects (all applied and verified live) |
|---|---|
| Tables | 5 — `emails` (520 rows) · `extracted_fields` (250) · `comparisons` (868) · `review_queue` (51) · `upload_runs` |
| Views | 4 — `submission_view` (**520 keys**, exactly the 5-key contract) · `dashboard_view` · `pipeline_stats` · `upload_stats` |
| Realtime | `emails`, `review_queue`, `comparisons` added to the `supabase_realtime` publication |
| Security | Row-level security enabled on all 5 tables; the service-role key never leaves the backend process |
| RPCs | `apply_manual_verdict()` · `revert_manual_verdict()` · `mark_submitted()` (+ helper/trigger functions) |
| Transport | `supabase` SDK when installed; otherwise a dependency-free `httpx` PostgREST channel with identical behaviour (this repository's dev machine never installed the SDK, and the cloud path still works end-to-end) |

Country score of that integration: the submission produced **from the cloud view** scores 1.0000 with
the same scorer — i.e. the database is on the critical path, not a decorative write.

### 3.4 Deterministic guardrails (why the score is stable)

- **Entity fields** — normalise (case, punctuation, legal-suffix noise, line collapsing), then compare; a fuzzy ratio is only accepted above **0.94**.
- **Ports** — split place names from UN/LOCODEs; a stale code is *conflict evidence*, never a matching key (real BLs routinely carry the wrong code with the correct port name).
- **Container count** — integer comparison, **zero tolerance**.
- **Gross weight** — unit-normalised to KG, accepted within **±10 kg or 0.2%, whichever is wider**.
- **One-sided blanks** — calibrated against the corpus rather than assumed: of 53 one-sided blanks only 2 were true defects, so a lone blank is *uncertain*, and is promoted to a defect only when **corroborated** by another confirmed defect in the same pair.
- **Escalation reasons** are ordered (confirmed mismatch ▸ attachment issue ▸ unreadable ▸ blank uncertainty), so a defect never gets buried under an escalation signal.
- **Uploads are isolated** — on-demand audits are journalled to `upload_runs` and can never enter the graded 520-key set (pinned by a regression test).

---

## 4. Business Value & Real-world ROI

The console computes ROI from *this batch's* own output (`backend/app/analytics.py`), and every
assumption is displayed on-screen and recomputable from environment variables.

| Headline card | Reference-run value | Derivation |
|---|---|---|
| **Token Cost Saved** | **$1.23 per 520-email batch** · 1,998,880 tokens avoided · **$46.90/yr** annualised | Fast-tracked emails never invoke a generative model — 520 avoided calls priced at the configured per-1M rates |
| **Time Saved Total** | **42.0 analyst hours per batch** (126 pairs × 20 min) vs **0.62 s** machine time · **$2,729.99** equivalent labour value | Measured machine time for the full 520-email pass, including reading all 126 attachment pairs |
| **Compliance Defects Caught** | **46 field-level mismatches**, defect sets matched exactly, zero false positives · 51 further pairs escalated rather than guessed | Official end-to-end scoreboard (46/46) |
| **Annualised impact** | ≈ **$104,046** total (tokens $46.90 + labour ≈ $103,999) · ≈ **1,600 analyst hours** released | Batch multiplier ×3.175 (400 pairs/month basis) × 12 months |

Assumptions are explicit and env-tunable, so a judge can change them live and watch the numbers move:

| Assumption | Env var | Default |
|---|---|---|
| Manual review effort per pair | `ROI_HUMAN_MINUTES_PER_PAIR` | 20 minutes |
| Loaded analyst cost | `ROI_USD_PER_HUMAN_HOUR` | $65 / hour |
| Monthly processing volume (annualisation basis) | `ROI_MONTHLY_PAIRS` | 400 pairs |
| Token pricing | `ROI_USD_PER_1M_INPUT` / `ROI_USD_PER_1M_OUTPUT` | $0.30 / $2.50 per 1M |

An honest framing we present to judges: **token savings are cents; the business case is analyst
hours and defects caught before they reach customs, the carrier or the LC.**

---

## 5. Production Runbook & One-Shot Commands

### 5.1 Prerequisites

- Python **3.11+** (stdlib only for the deterministic path), Node **18+** for the console, Docker optional (only for the organiser's scoring service).
- No API key is required to reproduce the 1.0000 baseline: without `GEMINI_API_KEY` the pipeline automatically runs the deterministic channel.

### 5.2 Bootstrap the dataset (one time)

```bash
make bootstrap          # unpack the two official archives → data/ , server/ , eval/private/
make verify             # sanity-check 520 emails / 250 attachments / submission key set
```

> `.gitignore` deliberately excludes `data/`, `server/`, `eval/`, `*.zip` and the answer key.
> The official ground truth is **never** committed — see §7.

### 5.3 Environment (`.env`, never committed)

```bash
cp .env.example .env
```

```ini
# --- AI channel (optional but recommended) ---
GEMINI_API_KEY=
GEMINI_MODEL_CLASSIFY=gemini-3.1-flash-lite
GEMINI_MODEL_EXTRACT=gemini-3.6-flash
GEMINI_MODEL_FALLBACK=gemini-3.5-flash
GEMINI_TIMEOUT_S=120
GEMINI_CONCURRENCY=8
GEMINI_CACHE=1

# --- Cloud (optional for local batch runs; required for the console's live features) ---
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=          # server-side only — never in frontend/

# --- Data source, scoring, services ---
INBOX_SOURCE=data
SUBMIT_URL=http://localhost:8080
API_PORT=8000
CORS_ORIGINS=http://localhost:3000
NEXT_PUBLIC_API_BASE=http://localhost:8000
```

`.env` is loaded by `backend/app/__init__.py` **before any sub-module import**, and never overrides
variables injected by the platform (Railway/Vercel semantics).

### 5.4 One-shot commands

| Goal | `make` | Raw command |
|---|---|---|
| Full test suite + regression gates | `make test` | `cd backend && python -m tests.run_all` |
| Official offline score | `make score-local` | `cd backend && python -m tests.test_submit --source file --offline` |
| Full 520-email audit (no API cost) | — | `cd backend && python -m app.runner --source data --no-llm` |
| Audit + write to Supabase | `make runs` | `cd backend && python -m app.runner --source data --no-llm --write-db` |
| Live smoke test (real Gemini + real cloud) | `make smoke` | `cd backend && python -m tools.live_smoke` |
| Multi-agent trace for one email | `make agents` | `cd backend && python -m app.agents.orchestrator --email email_031` |
| On-demand audit from the CLI | `make upload` | `cd backend && python -m app.upload <SI> <BL>` |
| Review console (Next.js) | `make web` | `cd frontend && npm install && npm run dev` → http://localhost:3000 |
| Verification API (FastAPI) | `make api` | `cd backend && python -m uvicorn app.main:app --port 8000` → /docs |
| Organiser scoring service | `make server` | `docker compose up --build` → http://localhost:8080 |

**No `make` on this machine?** Every target is a one-line wrapper — run the raw command from the table
directly (this is the normal path on Windows/Git Bash). Bootstrap has a PowerShell twin:

```powershell
powershell -File scripts\bootstrap.ps1     # equivalent to `make bootstrap`
```

### 5.5 Test suite

```bash
cd backend && python -m tests.run_all
```

**51 checks, all green** — 5 module self-checks (normaliser, judgement matrix, cloud gateway without the SDK, PostgREST channel, ROI formulas) plus 46 test cases across five suites:

| Suite | Cases | What it pins down |
|---|---|---|
| `tests.test_repo_contract` | 14 | Idempotent upserts, `on_conflict` matching the primary keys, generated columns, the exact 5-key contract |
| `tests.test_api` | 16 | Override write-back, invalid input rejected with 422, unreachable scorer → 503, ROI cards, live agent trace, and **the read-only guarantee** that live fetching never touches the graded payload |
| `tests.test_agents` | 5 | All four agents execute without errors, non-comparison mail short-circuits, trace evidence shape |
| `tests.test_stream` | 6 | Circular cursor, batch clamping, per-email failure isolation, live verdicts identical to the submission |
| `tests.test_dataset_regression` | 5 | Full 520-email pass scored by the organiser scorer, with metric floors |

### 5.6 Deployment notes (Vercel + Railway)

1. Start the API with `uvicorn app.main:app --host 0.0.0.0 --port $PORT` (binding to `127.0.0.1` fails health checks).
2. `python-multipart` is **required** — the upload endpoint returns 500 without it.
3. Set `CORS_ORIGINS` to the deployed frontend origin, and `NEXT_PUBLIC_API_BASE` to the deployed API origin.
4. `data/` and `eval/` are bootstrap products and are not in the image — run `scripts/bootstrap.sh` or mount a volume.
5. The console degrades gracefully: without a reachable API it still renders the snapshot-based review workflow (no blank screens, no dead spinners).

---

## 6. Repository Layout

```
backend/
  app/
    agents/      base · triage · extractor · verifier · escalation_judge · orchestrator (4-agent chain)
    pipeline/    classify (intent router) · extract (multimodal) · rule_extract · normalize ·
                 compare (judgement matrix) · policy (escalation ladder)
    llm/         gemini client (probe, backoff, cache, timeout) · schemas (Gemini-safe contracts) ·
                 prompts/classify.system.md · prompts/extract.system.md
    db/          supabase gateway · rest (dependency-free PostgREST channel) · repo (idempotent upserts)
    ingest/      inbox adapter (official loader) · readers (PDF/DOCX/XLSX/TXT with stdlib fallbacks)
    analytics.py (GBS ROI) · stream.py (read-only live fetch) · dashboard.py (snapshot export) ·
    upload.py (on-demand audit) · main.py (FastAPI)
  db/migrations/ 0001 enums · 0002 tables · 0003 views · 0004 realtime+RLS+RPC · 0005 upload runs
  tests/         run_all + 5 suites (+ test_submit scoreboard runner)
  tools/         live_smoke (real Gemini + real cloud verification)
frontend/        Next.js 14 console: verdict dashboard · 7-field diff · agent reasoning trace ·
                 ROI cards · live fetch · on-demand audit · human override
docs/            ARCHITECTURE_AND_AI.md (pitch narrative) · COMPLIANCE_AND_RUNBOOK.md (runbook, demo script)
scripts/         bootstrap · dataset verification
```

---

## 7. Engineering Log & Data Hygiene

### Defects the real corpus exposed (each now pinned by a test)

| Finding | Impact if shipped | Fix |
|---|---|---|
| Nothing in the codebase loaded `.env` — every module read `os.environ` | The AI channel silently degraded to rules while appearing "connected"; zero API calls, still a perfect score | Early `.env` loading in `app/__init__.py` that never overrides platform-injected variables |
| Prompt files referenced but never written to disk | Every LLM call died in `FileNotFoundError` inside a broad `except` — invisible | Prompts committed, plus `tools/live_smoke` as the only test that can prove the channel is live |
| Hard-coded model IDs retired by the provider (404 / quota 0) | Total outage on demo day; `models.list()` still advertised them | Candidate-chain probing with a 24 h cache and automatic failover |
| DOCX text extraction regex written for XLSX tags | 6 defect emails read as "image-only scans" — plausible and completely wrong | Correct tag handling, plus a regression test on real attachments |
| Label lookup keyed by normalised text but queried with raw matched text | BL vocabularies silently resolved to nothing (worked "by accident" on XLSX) | Key normalisation at the lookup boundary |
| Trailing `\b` in an alternation | 7 invoice queries misfiled into another intent | Fixed rules + a zero-false-positive assertion across all 520 subjects |
| Bulk writer persisted only 126 of 520 parent rows | Cloud submission would have been penalised for 394 missing keys | Full parent-row coverage; `submission_view` verified at exactly 520 keys |
| `httpx` does not raise on 4xx | Constraint rejections reported as successful writes | Explicit status handling (verified by inserting a row that violates a constraint) |

### Data hygiene — the red lines

- The official ground truth (`ground_truth.json`), both official archives, `data/`, `server/`, `eval/` and every `.env` are **git-ignored and never committed**.
- Pre-push guard:

  ```bash
  make check-leaks        # fails the build if an answer key, archive or .env is tracked
  ```

- The service-role key exists only inside the backend process; it is never returned by any endpoint and never reaches the browser bundle.
- Uploaded documents are journalled separately from the graded corpus, so a live demo cannot damage a submitted result.

### Scope & honest limits

- **In scope:** intent routing, 7-field extraction and comparison, escalation, human override, cloud persistence, real-time monitoring, on-demand audits.
- **Deliberately out of scope:** OCR of image-only scans — such attachments are escalated with an explicit reason instead of being guessed at, which is why escalation recall is a graded diagnostic.
- The AI's role is bounded by construction: it reads documents and routes intent; the verdict set that gets scored is produced deterministically. That is a feature, not a limitation — it is what makes a 1.0000 reproducible instead of lucky.

---

*Built for the Averis × Monash Hackathon 2026 GBS challenge. Reproduce the headline result with
`make test` and `make score-local`.*
