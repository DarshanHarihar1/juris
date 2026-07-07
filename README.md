# Juris

**Live, adversarial fact-checking for forwarded claims.** Paste a claim or a
WhatsApp-style forward; two AI investigators gather cited evidence, a jury of
independent models votes, and — if they disagree — the case goes to a live
adversarial trial (prosecutor vs. defense, judged on an anonymized transcript).
You get a citation-locked verdict, manipulation-technique tags, and a polite
forwardable rebuttal, all served at a permalink.

**Live:**
- App: https://juris-eta.vercel.app
- API: https://juris-web.onrender.com (`GET /health`)

v1 scope: **text input only** (image/audio/URL intake deferred), text-rendered
verdict cards (no PNG export), all models via the **NVIDIA NIM** free tier.

---

## How it works — the pipeline (S0→S6)

```
POST /api/verify
      │
      ▼
 S0  Intake            trim/normalize raw text
      │
      ▼
 S1  Normalize         detect language (en / hi / hi-Latn), extract check-worthy
      │                claims, strip opinions/greetings, split compound claims (≤3),
      │                produce an English pivot + native-language form
      ▼
 S2  Precedent         pgvector cosine search over the verified-claim cache
      │                (confidence ≥ 70, similarity ≥ 0.92) → instant reuse
      │                    │
      │                    └─ no hit → rated human fact-check (Google Fact Check API),
      │                       gated by embedding similarity ≥ 0.85 against THIS claim
      │                       (guards against fuzzy keyword false-matches)
      │                            │
      │                            └─ no hit → continue
      ▼
 S3  Investigate        2 tool-using agents (different model families) run parallel
      │                 ReAct loops — web_search, factcheck_search, source_credibility —
      │                 capped at 3 tool calls each, ≥1 disconfirming query required.
      │                 Evidence deduped by URL, stance + credibility scored.
      ▼
 S4  Fast-path jury     3 jurors (different families) read the evidence log (no
      │                 browsing) and vote. Confidence-weighted plurality ≥ 0.75
      │                 agreement → resolved by consensus.
      │                    │
      │                    └─ jury splits → escalate
      ▼                         │
 S6  Synthesize    ◄─────────── │
      │                         ▼
      │                    S5  Trial   prosecutor argues FALSE/MISLEADING, defense
      │                         argues TRUE, 2 rebuttal rounds, each may run one extra
      │                         search. Every factual sentence must carry an [e:id]
      │                         citation or it's stripped. A judge (unused family)
      │                         rules on an ANONYMIZED transcript ("Side 1"/"Side 2")
      │                         → 5-class verdict. 90s budget → expedited ruling.
      ▼
 VerdictCard: native-language one-liner + cited explanation, manipulation tags
 (9-item taxonomy), ≤400-char forwardable rebuttal, sources, models_used.
 Persisted to `verdicts`; confidence ≥ 70 seeds the S2 cache for future hits.
 Served at GET /api/verdicts/{slug} and streamed live via events_log.
```

Every stage emits an event row (`events_log`); the frontend subscribes via
Supabase Realtime and renders the trial as it happens — evidence cards,
jury result, escalation, argument bubbles, final verdict.

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
| Embeddings | `nvidia/nv-embedqa-e5-v5` (1024-dim) |
| Investigators (×2, parallel) | `meta/llama-3.1-8b-instruct`, `nvidia/nvidia-nemotron-nano-9b-v2` |
| Fast-path jury (×3) | `meta/llama-3.1-8b-instruct`, `deepseek-ai/deepseek-v4-flash`, `nvidia/nvidia-nemotron-nano-9b-v2` |
| Prosecutor / Defense | `deepseek-ai/deepseek-v4-pro` / `openai/gpt-oss-120b` |
| Judge | `moonshotai/kimi-k2-thinking` (family unused elsewhere, for impartiality) |
| Synthesizer | `meta/llama-3.1-8b-instruct` |

Exact IDs are read from `config.yaml` at runtime — swappable without a code
change. Small, fast tool-callers were chosen deliberately for investigators/
jury/synthesizer after live testing showed heavier models (dense 70B+,
`sarvam-m`) blew past request timeouts on the free tier; this cut end-to-end
latency from several minutes to under 90s without hurting verdict quality.

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
    pipeline/              s0_intake → s6_synthesize, orchestrator.py wires them
    services/              nim.py, search.py, tools.py, credibility.py, citations.py, jobs.py, events.py, whatsapp.py
    data/                  domains.yaml (credibility table), factcheckers.yaml (IFCN sites)
  migrations/              0001 schema, 0002 RLS, 0003 frontend/Realtime, 0004 WhatsApp
  tests/                   test_phase{0..5}.py — offline (mocked) + @needs_db/@needs_nim live tests
frontend/
  app/
    page.tsx               intake
    trial/[id]/page.tsx    live courtroom (Supabase Realtime)
    v/[slug]/page.tsx      SSR verdict permalink (SEO-crawlable)
  components/               StageRail, EvidenceCard, ArgumentBubble, VerdictCardView, RebuttalCard
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
| `GOOGLE_FACTCHECK_API_KEY` | Google Fact Check Tools API (precedent search) |
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
  project `juris`. `/v/[slug]` is server-rendered for SEO; `/trial/[id]`
  subscribes to Supabase Realtime with a 5s backfill poll for reconnects.
- **Database** — Supabase Postgres. Migrations in `backend/migrations/`,
  applied directly (no migration-runner service). RLS is on for every table;
  the backend uses the direct Postgres connection (bypasses RLS), while the
  frontend gets anon `SELECT` policies on `events_log`/`verdicts` only.

### Debugging a live deploy
```bash
render logs --resources <service-id> --limit 200 -o text | grep juris.
```
Every pipeline decision is logged under `juris.orchestrator` / `juris.s2` /
etc. — claim normalization, each S2 precedent candidate with its similarity
score and accept/reject reason, evidence counts, jury agreement, and the
final verdict + path. This was essential for catching a real bug where
Google Fact Check's fuzzy search matched an unrelated claim (fixed by adding
the S2 semantic-similarity gate — see `design/phase-2-precedent-search.md`).

---

## Build phases

Each phase is a vertical slice with its own goal, milestones, and testing
criteria — see `design/phase-N-*.md` for the full spec. All are implemented,
tested, and live-verified end-to-end.

| # | Phase | Covers |
|---|---|---|
| 0 | [Foundation & Infra](design/phase-0-foundation.md) | Supabase schema, Render services, NIM client, config matrix, job queue, events plumbing |
| 1 | [Intake & Normalize](design/phase-1-intake-normalize.md) | REST API, S0 text intake, S1 normalizer, orchestrator skeleton |
| 2 | [Precedent & Search Tools](design/phase-2-precedent-search.md) | S2 cache + Google FactCheck + SearXNG, tool framework, credibility scorer |
| 3 | [Investigation](design/phase-3-investigation.md) | S3 tool-using investigator agents (2), evidence log |
| 4 | [Verdict Engine](design/phase-4-verdict-engine.md) | S4 fast-path jury + agreement, S5 trial, citation validator |
| 5 | [Synthesis & Output](design/phase-5-synthesis-output.md) | S6 verdict card (text), rebuttal, manipulation tags, permalink |
| 6 | [Live Courtroom UI](design/phase-6-frontend.md) | Next.js app, Supabase Realtime stream, courtroom view, permalink page |
| 7 | [WhatsApp, Eval & Demo](design/phase-7-whatsapp-eval-demo.md) | Twilio WhatsApp adapter, golden-claim eval, `/stats`, demo video — **not started** |

## Known v1 limitations
- **Text only** — image/audio/URL submission returns `501` (`POST /api/media`).
- **Cache doesn't re-translate** — if a cached verdict (English) is reused for
  a same-meaning claim submitted in Hindi, the card is served as-is rather
  than re-synthesized in the requester's language.
- **No PNG export** — verdict cards are text-only (WhatsApp message + web page).
- **Free-tier cold starts** — Render's free web services and SearXNG spin down
  after ~15 min idle; the first request after that takes ~30–60s.

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
