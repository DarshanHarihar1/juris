# Juris

**Live fact-checking for forwarded claims.** Paste a claim or a WhatsApp-style
forward; one iterative verifier gathers cited evidence, decides whether to
search again or answer now, and returns a citation-locked verdict,
manipulation-technique tags, and a polite forwardable rebuttal at a permalink.

**Live:**
- App: https://juris-eta.vercel.app
- API: https://juris-web.onrender.com (`GET /health`)

Current scope: text, URL, and image intake; text-rendered verdict cards (no PNG
export), all models via the **NVIDIA NIM** free tier.

---

## How it works

```
POST /api/verify
      │
      ▼
 A  Normalize          detect language, extract up to 3 check-worthy claims,
      │                produce English pivot + native-language form, and assign
      │                a temporal profile (`is_time_sensitive`, `as_of_date`,
      │                `volatility`)
      ▼
 B  Verify             one iterative tool-calling verifier per claim; each loop
      │                chooses whether to `search`, `fetch_page`,
      │                `factcheck_search`, or answer with `final_verdict`
      │                while temporal validity is enforced in code
      ▼
 C  Synthesize         build the user-facing VerdictCard, persist it, and
                       deliver the permalink / WhatsApp reply
```

The live frontend subscribes to `events_log` via Supabase Realtime and renders
an investigation feed: claim, streamed evidence, verify steps, and final verdict.

---

## Architecture

| Layer | Tech | Where |
|---|---|---|
| Frontend | Next.js 14 (App Router), Tailwind, `@supabase/supabase-js` Realtime | Vercel |
| Backend | FastAPI (async), single process runs both the API and an in-process job-queue worker | Render (web service) |
| Meta-search | SearXNG (custom Docker image — JSON API enabled, limiter off) | Render (web service) |
| Database | Postgres + pgvector (embeddings, ivfflat cosine index), Realtime | Supabase |
| Models | NVIDIA NIM (OpenAI-compatible API), free tier | `build.nvidia.com` |
| Job queue | Plain Postgres table (`jobs`, `FOR UPDATE SKIP LOCKED`) — no Redis | Supabase |

**Why an in-process worker, not a separate service:** Render's free plan
doesn't offer background workers (Starter+ only). The FastAPI `lifespan` starts
`worker.run()` as an asyncio task alongside the API, so one free instance both
serves requests and drains the queue.

### Role → model matrix (`backend/app/config.yaml`)

All model calls go through NVIDIA NIM. Diversity of model *families* per role
is deliberate — an adversarial debate between different models resists
becoming an echo chamber:

| Role | Model(s) |
|---|---|
| Normalizer | `meta/llama-3.1-8b-instruct` |
| Verifier | `nvidia/nvidia-nemotron-nano-9b-v2` |
| Synthesizer | `meta/llama-3.1-8b-instruct` |
| OCR | `meta/llama-3.2-11b-vision-instruct` |

Exact IDs are read from `config.yaml` at runtime. The verifier is optimized for
tool-calling and long-lived claim context rather than multi-model diversity.

---

## Repo layout

```
backend/
  app/
    main.py              FastAPI app: /api/verify, /api/jobs/{id}/events, /api/verdicts/{slug}, /webhooks/whatsapp
    worker.py             in-process job-queue poll loop
    config.py / config.yaml   role→model matrix, thresholds
    models.py             Pydantic schemas (Submission, Claim, VerdictCard, ...)
    db.py                  asyncpg pool (Supabase pooler-safe)
    pipeline/              intake, normalize, verify, synthesize; orchestrator.py wires them
    services/              nim.py, search.py, tools.py, credibility.py, citations.py, jobs.py, events.py, whatsapp.py
    data/                  domains.yaml (credibility table), factcheckers.yaml (IFCN sites)
  migrations/              0001 schema, 0002 RLS, 0003 frontend/Realtime, 0004 WhatsApp, 0006 v2 verdict/path widening
  tests/                   v2-focused offline (mocked) + @needs_db live tests
frontend/
  app/
    page.tsx               intake
    investigation/[id]/page.tsx    live investigation feed (Supabase Realtime)
    trial/[id]/page.tsx            compatibility alias to investigation
    v/[slug]/page.tsx      SSR verdict permalink (SEO-crawlable)
  components/               StageRail, EvidenceCard, VerdictCardView, RebuttalCard
  lib/                      types, Supabase client, API config, citation renderer
searxng/                   custom Dockerfile + settings.yml (JSON API, limiter off)
design/                    phase-by-phase spec docs (design/phase-N-*.md) + HLD/LLD
render.yaml                Render Blueprint (web + searxng services)
```

---

## Local development

### Backend
```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example ../.env   # fill in DATABASE_URL, NIM_API_KEY, etc. (see below)
uvicorn app.main:app --reload
```

### Frontend
```bash
cd frontend
npm install
cp .env.example .env.local   # fill in NEXT_PUBLIC_* vars
npm run dev
```

### Tests
```bash
cd backend
pytest tests/ -q                              # offline tests only (no keys needed)
DATABASE_URL=... NIM_API_KEY=... pytest tests/ -q   # full suite incl. live DB/NIM tests
```
Tests are organized per phase (`test_phase0.py` … `test_phase5.py`), mirroring
`design/phase-N-*.md`. Tests requiring a live DB or NIM key skip cleanly via
`@needs_db` / `@needs_nim` markers when the corresponding env var is absent.

---

## Environment variables

**Backend** (`.env` at repo root, loaded by `app/config.py` and `tests/conftest.py`):

| Var | Purpose |
|---|---|
| `DATABASE_URL` | Supabase Postgres connection (session pooler; pool capped at 5 to stay under the free-tier 15-client limit) |
| `NIM_API_KEY` | NVIDIA NIM auth |
| `GOOGLE_FACTCHECK_API_KEY` | Google Fact Check Tools API (used by the verifier's `factcheck_search` tool) |
| `SEARXNG_URL` | Internal URL of the SearXNG service (`web_search` tool) |

**Frontend** (`frontend/.env.local` / Vercel project env):

| Var | Purpose |
|---|---|
| `NEXT_PUBLIC_API_URL` | Backend base URL (Render) |
| `NEXT_PUBLIC_SUPABASE_URL` | Supabase project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Supabase anon/publishable key (safe to expose — read-only Realtime on `events_log`, gated by RLS) |

---

## Deployment

- **Backend + SearXNG** — Render Blueprint (`render.yaml`), auto-deploys on
  push to `main`. Both are `type: web` (Render's free plan has no background
  worker or private-service tier); SearXNG runs a custom Dockerfile since the
  stock image ignores env-var setting overrides and ships its API-blocking
  rate limiter on.
- **Frontend** — Vercel, deployed via the Vercel CLI (`vercel deploy --prod`),
  project `juris`. `/v/[slug]` is server-rendered for SEO; `/investigation/[id]`
  subscribes to Supabase Realtime with a 5s backfill poll for reconnects. The
  legacy `/trial/[id]` route remains as a compatibility alias.
- **Database** — Supabase Postgres. Migrations in `backend/migrations/`,
  applied directly (no migration-runner service). RLS is on for every table;
  the backend uses the direct Postgres connection (bypasses RLS), while the
  frontend gets anon `SELECT` policies on `events_log`/`verdicts` only.

### Debugging a live deploy
```bash
render logs --resources <service-id> --limit 200 -o text | grep juris.
```
Every pipeline decision is logged under `juris.*` — claim normalization,
verify steps, evidence counts, tool usage, and the final verdict path.

---

## Current limitations
- **Audio is still unsupported** — `POST /api/verify` returns `501` for `type="audio"`.
- **No PNG export** — verdict cards are text-only (WhatsApp message + web page).
- **Free-tier cold starts** — Render's free web services and SearXNG spin down
  after ~15 min idle; the first request after that takes ~30–60s.

## Research foundations

The v2 single-verifier pipeline is grounded in published work on retrieval-augmented
fact-checking, iterative retrieval, and temporal validity. Key references:

| Paper | What we took from it |
|---|---|
| [Loki / OpenFactVerification](https://arxiv.org/abs/2410.01794) | Simple linear pipeline, explicit check-worthiness filtering, and query reformulation around the underlying fact instead of the claim's wording |
| [FIRE: Fact-checking with Iterative Retrieval and Verification](https://arxiv.org/abs/2411.00784) | Single agent deciding whether to answer now or search again; the core shape of the Verify loop |
| [AVeriTeC shared task 2025](https://aclanthology.org/2025.fever-1.15/) | Competitive evidence that simple single-agent / RAG fact-checking pipelines outperform heavyweight multi-agent debates |
| [Temporal failure modes in statutory QA](https://arxiv.org/abs/2605.23497) | Hard temporal filtering by as-of date matters more than prompt hints for time-sensitive claims |
| [SemanticCite](https://arxiv.org/abs/2511.16198) | Verdicts should be grounded in fetched page content, not snippets alone |
| [OpenFactCheck](https://arxiv.org/abs/2408.11832) | Retrieval silence on static claims is weak evidence, supporting the constrained parametric fallback for non-time-sensitive facts |

## Global conventions
- **Models:** all via NVIDIA NIM (`https://integrate.api.nvidia.com/v1`,
  OpenAI-compatible, `Bearer $NIM_API_KEY`). Role→model matrix in
  `backend/app/config.yaml`.
- **Rate limit:** ~40 req/min free tier → global semaphore in `services/nim.py`,
  plus a 45s per-request timeout with fallback-model retry.
- **Structured output everywhere:** every model call validates against a
  Pydantic schema; retry ×1 on invalid, then degrade/fallback.
- **Cost:** $0 — every service used is on a free tier. The budget managed is
  requests/min, not dollars.
