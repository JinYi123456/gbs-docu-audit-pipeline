# CargoMatrix — SDOC SI vs BL Verification Console

**User Manual & Operations Handbook**

Ocean-freight document verification: classify inbound shipping email → extract the 7
canonical fields from the SI and draft-BL attachments → cross-verify → escalate what the
machine cannot decide → human review closes the loop.

---

## 1. System at a glance

```
┌──────────────────┐   fetch    ┌──────────────────────┐   SDK    ┌──────────────┐
│  Vercel (Next.js)│ ────────▶ │  Railway (FastAPI)   │ ───────▶ │   Supabase   │
│  review console  │ ◀──────── │  verification API    │ ◀─────── │  Postgres    │
└──────────────────┘   JSON    └──────────┬───────────┘          └──────────────┘
                                          │ optional
                                          ▼
                                   Google Gemini (multimodal extraction)
```

| Layer | What it does | Where the secrets live |
|---|---|---|
| **Frontend** (Vercel) | Read-only audit console + upload & live-pull interactions. Zero keys. | Nowhere — it only talks to its own API. |
| **Backend** (Railway) | All business logic: pipeline, agent orchestration, human-review writes, scoring contract. | `GEMINI_API_KEY`, `SUPABASE_*` stay inside this process only. |
| **Database** (Supabase) | 520 official emails, comparisons, review queue, upload runs. Views feed the official submission. | service key access only from the backend. |

### Live URLs

| What | URL |
|---|---|
| Web console (production) | https://gbs-docu-audit-pipeline.vercel.app |
| API health | https://gbs-docu-audit-pipeline-production.up.railway.app/health |
| API base | https://gbs-docu-audit-pipeline-production.up.railway.app |

> The API root path `/` intentionally returns `{"detail":"Not Found"}` — the backend only
> serves `/health` and `/api/*` endpoints. That is not an error.

---

## 2. Running the project from zero (local machine)

### 2.1 Prerequisites — install once

| Tool | Version | Where to get it |
|---|---|---|
| **Git** | any recent | https://git-scm.com |
| **Python** | 3.13 recommended (3.11+) | https://python.org — tick "Add to PATH" on Windows |
| **Node.js** | 20 LTS or newer | https://nodejs.org |
| **VS Code** (optional but recommended) | latest | https://code.visualstudio.com |

Verify in a terminal (PowerShell or Git Bash):

```bash
git --version
python --version     # 3.13.x
node --version       # v20.x or newer
npm --version
```

### 2.2 Get the code

```bash
git clone https://github.com/JinYi123456/gbs-docu-audit-pipeline.git
cd gbs-docu-audit-pipeline
```

### 2.3 Configure secrets — `.env` at the repo root

Create a file named `.env` (never commit it; `.gitignore` already excludes it):

```ini
GEMINI_API_KEY=your_gemini_key_from_aistudio
SUPABASE_URL=https://ktxcchtdzkijmkihyyov.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your_sb_secret_key
```

Notes

- Without `GEMINI_API_KEY` everything still runs — the pipeline silently uses the
  deterministic rule channel (offline-safe by design).
- Without the Supabase pair, the audit pages fall back to the local snapshot file and
  review overrides are stored in a local JSON file instead of the cloud.
- The same variables exist in **Railway → your service → Variables** for production.
  Local `.env` never overrides platform-injected variables.

### 2.4 Backend — install & run

```bash
cd backend
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Git Bash / macOS:
source .venv/Scripts/activate      # mac/Linux: source .venv/bin/activate

pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000
```

- API docs (Swagger UI): http://localhost:8000/docs
- Health: http://localhost:8000/health

Optional but recommended — the official data bundle (enables the *full* local pipeline
for upload & live pull; audit pages work without it):

```bash
make bootstrap          # from the repo root; unpacks the official data/ directory
```

### 2.5 Frontend — install & run (second terminal)

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:3000 — the console reads the local backend on port 8000 by default
(configurable via `NEXT_PUBLIC_API_BASE`).

### 2.6 Run the test suites

```bash
# backend (46 contract tests + 5 cloud-path regression tests)
cd backend && python -m pytest tests/ -q

# frontend
cd frontend && npm run typecheck
```

---

## 3. Using the web console — feature by feature

### 3.1 Dashboard & KPI cards

Open the console. The top row shows the three ROI cards (time saved, human minutes
avoided, machine seconds), the category mix and the defect breakdown — all derived from
the same verification run that produced the submission, not from a separate decorative
calculation.

### 3.2 Audit queue

The main table lists all **520** official emails. You can:

- **Filter** by status (`OK` / `MISMATCH` / `NEEDS_REVIEW`) or category
  (`BL_COMPARISON`, `SI_REQUEST`, `INVOICE_QUERY`, `GENERAL`, `SPAM`).
- **Search** fuzzy across email ID, subject and sender.
- Toggle **only defects** to see the 46 mismatched emails.

Expected result: `total: 520`; mismatches: 46; review queue: 51 items.

### 3.3 Email detail — the 7-field comparison

Click any row. For BL-comparison emails with attachments you get the SI vs draft-BL
side-by-side grid over the 7 canonical fields:

`Shipper · Consignee · Notify Party · Port of Loading · Port of Discharge ·
Container Count · Gross Weight (kg)`

Row colours: **red = mismatched**, **yellow = undecided (needs human)**, **green =
matched**. Normalised values and similarity are shown under each raw value.

> In the cloud-only environment the per-field grid shows the "no comparable fields"
> placeholder — attachment bytes are not stored server-side; verdicts come from the
> archived pipeline run.

### 3.4 Live pull (stream)

Click **Pull next batch** (stream panel). Every click takes the next batch
(round-robin cursor, default 6 emails) from the pool of **126 comparable BL emails** and
shows per-email verdicts with a 4-step agent trace. Channel badge:

- `deterministic` — rule channel, millisecond-fast (default).
- `cloud-replay` — production mode; replays archived authoritative verdicts from the
  database (read-only, cannot dirty the 520-key submission).
- `gemini` — when the multimodal toggle is on locally (2–14 s per email).

### 3.5 Upload & verify (the core interaction)

Open the upload panel, load or drop an **SI** and a **draft BL** (txt/pdf/docx), then
press **Verify**. What you get:

1. Category + confidence (triage).
2. Per-field extraction for both documents (multimodal when a Gemini key is configured,
   rule channel otherwise).
3. The full 7-field comparison matrix with red/yellow/green highlighting.
4. The final verdict: `OK`, `MISMATCH (fields...)`, or `NEEDS_REVIEW (reason...)`.
5. A live agent trace you can expand — this is the same computation that would be
   persisted for a real inbound email.

Upload runs are recorded to `upload_runs` in Supabase and are **strictly isolated** from
the official 520-email evaluation set.

### 3.6 Human review — closing the loop

On any `NEEDS_REVIEW` or `MISMATCH` row, apply a manual verdict:

- Choose the final status, pick the defect fields (from the 7 canonical ones only),
  add an optional note and reviewer name.
- The override is written to Supabase (via the `apply_manual_verdict` RPC), the row is
  immediately badged as **overridden**, and the submission preview updates.
- **Revert** restores the machine verdict.

Everything is validated server-side: invalid field names, `MISMATCH` without defects and
other contract violations are rejected with HTTP 422 before anything is stored.

### 3.7 Submission preview

`GET /api/submission` (and the console's submission panel) shows the official 5-key
payload — exactly 520 records, each with `category / status / review_reason /
has_defect / defect_fields`, overrides applied, `problems: []` when the contract is
clean. This is the artifact the official scorer grades.

---

## 4. Deployment & operations

### 4.1 How deploys happen

| Platform | Trigger | What to watch |
|---|---|---|
| **Railway** | every push to `main` (backend lives in `/backend`, root directory setting) | Deploy logs; healthcheck `/health` |
| **Vercel** | every push to `main` (frontend lives in `/frontend`) | Build logs; the deployed page |

### 4.2 Environment variables (production)

**Railway** (service → Variables):

| Variable | Value |
|---|---|
| `SUPABASE_URL` | `https://ktxcchtdzkijmkihyyov.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | the new-generation `sb_secret_…` key |
| `GEMINI_API_KEY` | your AI Studio key |
| `INBOX_SOURCE` | `data` |
| `INBOX_DATA_DIR` | `data` |
| `CORS_ORIGINS` | `https://gbs-docu-audit-pipeline.vercel.app,http://localhost:3000` |

**Vercel** (project → Settings → Environment Variables):

| Variable | Value |
|---|---|
| `NEXT_PUBLIC_API_BASE` | `https://gbs-docu-audit-pipeline-production.up.railway.app` |

> ⚠️ **Iron rule:** every `NEXT_PUBLIC_*` change is inlined at **build time** — after
> changing it you must trigger a fresh Vercel deployment (an empty commit is enough:
> `git commit --allow-empty -m "chore: rebuild" && git push`), otherwise nothing changes.

### 4.3 Health & troubleshooting

```bash
curl https://gbs-docu-audit-pipeline-production.up.railway.app/health
```

Healthy response contains `"status":"ok"`, `"source":"supabase"`,
`"supabase":{"sdk_installed":true,"configured":true}`, `"gemini_key":"set"`.

| Symptom | Meaning | Fix |
|---|---|---|
| Yellow banner "Realtime services are unreachable …" | The page could not reach the API it was built against. | Hard-refresh (Ctrl+Shift+R) / new tab; check `NEXT_PUBLIC_API_BASE` + redeploy; check Railway `/health`. |
| `502 Application failed to respond` during a deploy window | Railway is swapping containers. | Wait ~1–2 minutes and retry. |
| `503 No verification snapshot available … [cloud read failed: …]` | No local snapshot AND the cloud read failed — the bracketed reason names the cause. | Read the reason; usually an env-var or DB issue. |
| `422` on review submit | Input violates the contract (e.g. `MISMATCH` without defect fields). | Fix the form values shown in the error detail. |
| Console shows old data | Browser tab has been open since an old deployment. | Hard refresh. |

### 4.4 Database migrations

Schema is codified in `backend/db/migrations/0001…0006*.sql` (0006 = security
hardening). Supabase and the repository stay in sync; to rebuild a project from zero,
apply the migration files in order.

---

## 5. FAQ

**Q: Do I need VS Code for the AI features to work?**
No. The Gemini API key lives in the **backend** process (Railway in production, `.env`
locally). The AI is invoked server-side by the verification pipeline. Any browser — on
your machine or anyone's — gets the full AI-backed results through the public console.
VS Code is simply where you develop; Vercel's public page is not limited to "basic
testing". What the public page never exposes is the key itself (BFF pattern: the browser
only talks to your API).

**Q: Is the public site safe to demo?**
Yes: the frontend holds no secrets, database access is service-key only from the
backend, RPCs are locked to `service_role`, views run with `security_invoker`, and the
official submission is read-only from the UI's perspective.

**Q: Where do the 520 emails come from?**
The official GBS dataset. Git tracks the pipeline and schema, not the raw bundle
(`data/inbox` stays local via `.gitignore`); the **results** live in Supabase.

**Q: What exactly is scored?**
Stage-1 macro-F1 over the 5 categories, end-to-end exact match of the defect-field
sets, and reliability of escalations — computed by the official `server/scoring.py`
against the 5-key submission.
