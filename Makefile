# SDOC Hackathon 2026 · 统一入口
# Windows 上没有 make 时，直接执行等价的 python 命令（见 .freebuff/run.md）。

ROOT := $(CURDIR)
PY ?= python3 -c "import sys; print(sys.executable)" >/dev/null 2>&1 && python3 || python

.PHONY: help bootstrap verify check-leaks api server web run run20 runs score score-local test smoke agents upload freeze clean

help:
	@echo "SDOC verification pipeline — entry points"
	@echo "  make bootstrap     unpack the two official archives into data/ server/ eval/ (idempotent)"
	@echo "  make verify        check 520 emails / 250 attachments / submission key set"
	@echo "  make test          unit tests + metric-threshold regression gates"
	@echo "  make score-local   official scorer, offline (prints the five headline metrics)"
	@echo "  make score         self-scoring (HTTP service first, offline scorer as fallback)"
	@echo "  make run           full 520-email audit -> eval/report/submission.json"
	@echo "  make run20         20-email smoke batch"
	@echo "  make runs          full audit + write to Supabase (requires .env)"
	@echo "  make smoke         live smoke test: real Gemini + real cloud (spends API quota)"
	@echo "  make agents        replay the multi-agent trace (EMAIL=email_031)"
	@echo "  make upload        on-demand audit from the CLI (FILES=\"si.pdf bl.pdf\")"
	@echo "  make server        organiser scoring service on http://localhost:8080 (needs Docker)"
	@echo "  make api           our FastAPI verification service on :8000 (/docs)"
	@echo "  make web           review console on http://localhost:3000"
	@echo "  make check-leaks   pre-push guard: answer key / archives / .env must not be tracked"

bootstrap:
	bash scripts/bootstrap.sh

verify:
	python3 scripts/verify_dataset.py

check-leaks:
	@git ls-files | grep -Ei 'ground_truth|\.zip$$|\.env$$' && echo "!! 泄漏：以上文件已入库 !!" && exit 1 || echo "干净：答案键 / zip / .env 均未入库"

server:
	docker compose up --build

api:
	cd backend && python3 -m uvicorn app.main:app --reload --port 8000

web:
	cd frontend && npm run dev

run:
	cd backend && python3 -m app.runner --source data --write-db

run20:
	cd backend && python3 -m app.runner --source data --limit 20 --concurrency 4

runs:
	cd backend && python3 -m app.runner --source data --no-llm --write-db

# 实弹烟测：真调 Gemini（分类 1 次 flash + 提取 1 次 pro 档）+ 真探 Supabase
smoke:
	cd backend && python3 -m tools.live_smoke

# 多 Agent 轨迹复盘：EMAIL=email_031 make agents
EMAIL ?= email_031
agents:
	cd backend && python3 -m app.agents.orchestrator --email $(EMAIL)

# 上传即核对（命令行版）：FILES="si.pdf bl.pdf" make upload
FILES ?=
upload:
	cd backend && python3 -m app.upload $(FILES)

score:
	cd backend && python3 -m tests.test_submit --source auto

score-local:
	cd backend && python3 -m tests.test_submit --source file --offline

test:
	cd backend && python3 -m tests.run_all

clean:
	rm -rf .cache backend/app/__pycache__ backend/tests/__pycache__ frontend/.next
