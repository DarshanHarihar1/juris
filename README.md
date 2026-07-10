# Juris

**Live fact-checking for forwarded claims.** Paste a claim or a WhatsApp-style
forward; one iterative verifier gathers cited evidence, decides whether to
search again or answer now, and returns a citation-locked verdict,
manipulation-technique tags, and a polite forwardable rebuttal at a permalink.

**Live:**
- App: https://juris-eta.vercel.app
- API: https://juris-web.onrender.com (`GET /health`)
- WhatsApp: Text claims to +1-480-XXX-XXXX (Twilio sandbox)

Current scope: text, URL, and image intake; text-rendered verdict cards (no PNG
export), all models via **MeshAPI**.

---

## How it works

```
POST /api/verify
      │
      ▼
 S0  Intake             detect content type, extract text/image
      │
      ▼
 S1  Normalize          detect language, extract up to 3 check-worthy claims,
      │                produce English pivot + native-language form, and assign
      │                a temporal profile (`is_time_sensitive`, `as_of_date`,
      │                `volatility`)
      ▼
     Verify             one iterative tool-calling verifier per claim; each loop
      │                chooses whether to `search`, `fetch_page`,
      │                `factcheck_search`, or answer with `final_verdict`
      │                while temporal validity is enforced in code
      ▼
     Synthesize         build the user-facing VerdictCard, persist it, and
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
| Models | MeshAPI (OpenAI-compatible, unified router) | `api.meshapi.ai` |
| Job queue | Plain Postgres table (`jobs`, `FOR UPDATE SKIP LOCKED`) — no Redis | Supabase |
| Integrations | Twilio (WhatsApp), Google Fact Check API, LangSmith (tracing) | Various |

**Why an in-process worker, not a separate service:** Render's free plan
doesn't offer background workers (Starter+ only). The FastAPI `lifespan` starts
`worker.run()` as an asyncio task alongside the API, so one free instance both
serves requests and drains the queue.

### Role → model matrix (`backend/app/config.yaml`)

All model calls go through MeshAPI, a single OpenAI-compatible router with
unified billing across providers.

| Role | Model |
|---|---|
| Normalizer | `openai/gpt-oss-120b` |
| Verifier | `openai/gpt-oss-120b` |
| Synthesizer | `openai/gpt-oss-120b` |
| OCR | `google/gemma-3-4b-it` — cheapest MeshAPI model that accepts image input |

Exact IDs are read from `config.yaml` at runtime. The verifier is optimized for
tool-calling and long-lived claim context.

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
    pipeline/              intake (s0), normalize (s1), verify, synthesize; orchestrator.py wires them
      s0_intake.py         request parsing, content-type detection
      s1_normalize.py      language detection, claim extraction
      verify.py            iterative tool-calling verifier loop
      synthesize.py        verdict card builder + permalink generation
      orchestrator.py      pipeline orchestration
    services/              mesh.py (MeshAPI client), search.py, tools.py, credibility.py, citations.py, jobs.py, events.py, whatsapp.py
    data/                  domains.yaml (credibility table), factcheckers.yaml (IFCN sites)
  migrations/              0001 schema, 0002 RLS, 0003 frontend/Realtime, 0004 WhatsApp, 0005 QA evidence, 0006 v2 verdict, 0007 drop trials
  tests/                   v2-focused offline (mocked) + @needs_db live tests
frontend/
  app/
    page.tsx               intake
    investigation/[id]/page.tsx    live investigation feed (Supabase Realtime)
    trial/[id]/page.tsx            compatibility alias to investigation
    v/[slug]/page.tsx      SSR verdict permalink (SEO-crawlable)
  components/               StageRail, EvidenceCard, VerdictCardView, RebuttalCard, etc.
  lib/                      types, Supabase client, API config, citation renderer
searxng/                   custom Dockerfile + settings.yml (JSON API, limiter off)
design/                    phase-by-phase spec docs (design/phase-N-*.md) + HLD/LLD
render.yaml                Render Blueprint (web + searxng services)
docker-compose.yml         Local dev: Postgres, Supabase emulator (optional)
```

---

## Local development

### Backend
```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example ../.env   # fill in DATABASE_URL, MESH_API_KEY, etc. (see below)
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
DATABASE_URL=... MESH_API_KEY=... pytest tests/ -q   # full suite incl. live DB/MeshAPI tests
```
Tests are organized per phase (`test_phase0.py` … `test_phase6.py`), mirroring
`design/phase-N-*.md`. Tests requiring a live DB or MeshAPI key skip cleanly via
`@needs_db` / `@needs_mesh` markers when the corresponding env var is absent.

---

## Environment variables

**Backend** (`.env` at repo root, loaded by `app/config.py` and `tests/conftest.py`):

| Var | Purpose |
|---|---|
| `DATABASE_URL` | Supabase Postgres connection (session pooler; pool capped at 5 to stay under the free-tier 15-client limit) |
| `MESH_API_KEY` | MeshAPI key (sole model provider for normalizer, verifier, synthesizer, and OCR) |
| `GOOGLE_FACTCHECK_API_KEY` | Google Fact Check Tools API (used by the verifier's `factcheck_search` tool) |
| `SEARXNG_URL` | URL of the SearXNG meta-search service (`web_search` tool) |
| `TWILIO_ACCOUNT_SID` | Twilio account ID for WhatsApp integration |
| `TWILIO_AUTH_TOKEN` | Twilio auth token for WhatsApp integration |
| `TWILIO_WHATSAPP_FROM` | Twilio WhatsApp sender number (format: `whatsapp:+1...`) |
| `WA_HASH_SALT` | Salt for per-user WhatsApp hash generation (must be constant for consistent user tracking) |
| `LANGSMITH_API_KEY` | LangSmith API key for tracing (optional; if absent, tracing is disabled) |

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
render logs --resources juris-web --limit 200 -o text | grep juris.
```
Every pipeline decision is logged under `juris.*` — claim normalization,
verify steps, evidence counts, tool usage, and the final verdict path.

### GitHub Actions workflows (`.github/workflows/`)

Both are cron-triggered keep-alive pings — Render's free tier spins services
down after ~15 min idle and Supabase auto-pauses a project after 7 days idle.

| Workflow | Schedule | Purpose |
|---|---|---|
| `health-cron.yml` | daily, 06:00 UTC | Curls `juris-web`'s `/health`; keeps Supabase from auto-pausing and warms the Render web service. Reads the `HEALTH_URL` repo secret. |
| `searxng-warm.yml` | every 14 min | Curls SearXNG with a throwaway query, retrying up to 4× on cold-start 502s. Reads the optional `SEARXNG_PING_URL` repo secret (defaults to the public SearXNG URL). |

Both also support `workflow_dispatch` for a manual run from the Actions tab.

---

## Current limitations
- **Audio is still unsupported** — `POST /api/verify` returns `501` for `type="audio"`.
- **No PNG export** — verdict cards are text-only (WhatsApp message + web page).
- **Free-tier cold starts** — Render's free web services and SearXNG spin down
  after ~15 min idle; the first request after that takes ~30–60s.
- **WhatsApp sandbox** — currently on Twilio sandbox; production setup requires Business Account verification.

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
- **Models:** all via MeshAPI (`https://api.meshapi.ai/v1`, OpenAI-compatible, `Bearer $MESH_API_KEY`). Role→model matrix in `backend/app/config.yaml`.
- **Rate limit:** managed by request backoff in `services/mesh.py`.
- **Structured output everywhere:** every model call validates against a Pydantic schema; retry ×1 on invalid, then degrade/fallback.
- **Cost:** $0 — every service used is on a free tier. The budget managed is requests/min, not dollars. 
