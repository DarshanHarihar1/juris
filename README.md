# Juris

**Live fact-checking for forwarded claims.** Paste a claim or a WhatsApp-style
forward; one iterative verifier gathers cited evidence, decides whether to
search again or answer now, and returns a citation-locked verdict,
manipulation-technique tags, and a polite forwardable rebuttal at a permalink.

**Live:**
- App: https://juris-eta.vercel.app

**Examples:** see [EXAMPLES.md](EXAMPLES.md) — real text and image claims with
links to their live investigations and verdicts.

---

## How it works

A verification request is asynchronous end-to-end: the API enqueues work and
returns immediately; an in-process worker drains the queue and runs the
pipeline; the frontend (or WhatsApp webhook) observes progress through
`events_log` rows streamed via Supabase Realtime.

### Request lifecycle

```
Client                    API (FastAPI)                 Worker (asyncio)
  │                            │                              │
  │  POST /api/verify          │                              │
  │  {type, content}           │                              │
  ├───────────────────────────►│  insert submissions row      │
  │                            │  enqueue jobs row (queued)   │
  │◄───────────────────────────┤  return {job_id, url}        │
  │                            │                              │
  │  subscribe events_log      │         claim_next()         │
  │  (Supabase Realtime)       │         (SKIP LOCKED)        │
  │                            │                              ├─► orchestrator.run()
  │◄── stage / claim /         │                              │     Intake → Normalize → Verify×N → Synthesize
  │    evidence / verdict      │                              │
  │                            │                              ├─► mark_done / mark_error
```

Supported intake types: `text` (inline claim), `url` (page fetched and stripped
to text at intake), and `image` (OCR via a vision model at intake).

### Pipeline stages

```
POST /api/verify  →  jobs queue  →  orchestrator.run()
                                           │
                                           ▼
                                    Intake
                                           │
                                           ▼
                                    Normalize
                                           │
                          ┌────────────────┼────────────────┐
                          ▼                ▼                ▼
                     Verify (claim 1)  Verify (claim 2)  Verify (claim N)   ← asyncio.gather, max 3 claims
                          │                │                │
                          └────────────────┼────────────────┘
                                           ▼
                                    Synthesize (Verdict)
                                           │
                                           ▼
                              persist verdicts + events_log
                              deliver WhatsApp reply (if channel=whatsapp)
```

#### Intake

Resolves every submission to plain text before any LLM runs.

| `media_type` | Resolution |
|---|---|
| `text` | Use `raw_text` as-is |
| `url` | HTTP GET → strip `<script>`/`<style>`/tags → unescape entities via regex |
| `image` | OCR via MeshAPI vision model (`google/gemma-3-4b-it`); accepts `data:image/...` or public HTTPS URLs |

Output is whitespace-collapsed and capped at 5,000 characters. Empty intake
emits a terminal event (`nothing_to_verify`) and optionally replies on WhatsApp.

#### Normalize

Two steps:

1. **Language detect** — `langdetect` in-process (deterministic seed); optional `lang_hint` from the client wins.
2. **Claim extraction** — one structured LLM call (`normalizer` role) that strips greetings, opinions, CTAs, and noise, then emits up to **3** atomic, check-worthy factual sub-claims copied nearly verbatim from the message.

If no sub-claims survive filtering, the job terminates early with
`nothing_to_verify`.

#### Verify — iterative FIRE-style agent (one per sub-claim)

Each sub-claim gets its own verifier loop, running in parallel when there are
multiple claims. The agent has a single tool:

- **`search`** — SearXNG meta-search (top 5 by score) → page fetch for full
  text (Trafilatura first, in-process HTML parse; falls back to Jina Reader
  for JS-heavy/paywalled pages that come back empty or too thin) → evidence
  rows emitted to `events_log` as they arrive.

When ready, the agent replies with structured JSON validated against
`SubClaimVerdict`:

```json
{"verdict": "true"|"false"|"unverifiable", "explanation": "...", "evidence": ["https://..."]}
```

**Loop controls** (from `config.yaml` thresholds):

| Guard | Behavior |
|---|---|
| Step budget | Max 6 tool rounds (`max_verify_steps`); then a forced structured verdict call |
| Time budget | 75 s wall-clock per claim (`verify_budget_s`) |
| Temporal guard | Time-sensitive claims (office-holders, "current", dated facts) cannot settle `true`/`false` without at least one retrieved URL in `evidence` |
| False guard | `false` verdicts that rest on *absence* of confirmation ("no evidence found…") are downgraded to `unverifiable` |
| Schema retry | Invalid JSON → nudge + retry (×2); mesh.call validates via Pydantic |
| Context trim | Prior tool-result blocks in the message history are stubbed to keep context bounded; cited URLs live in `evidence_log` |

Parametric knowledge is allowed for stable, general facts on non-time-sensitive
claims; the system prompt injects today's date so the model knows its cutoff.

#### Synthesize — Verdict card

Combines per-claim results into one user-facing `VerdictCard`:

| Sub-claims | Combination | LLM use |
|---|---|---|
| 1 | Direct format of verifier output | Template formatting |
| 2–3 | Rule-based **AND** (`false` dominates; all `true` → `true`; else `unverifiable`) | One `synthesizer` call for a unified explanation in the detected language |

The card includes slug (permalink), confidence score, evidence refs (up to 5
URLs), one-liner, explanation, and a ≤400-char forwardable rebuttal. Persisted
to `verdicts`; a `verdict` event is emitted for the live feed. WhatsApp
submissions get the card delivered via Twilio after persistence.

### Live investigation feed

The Next.js investigation page subscribes to `events_log` filtered by `job_id`
through Supabase Realtime (anon key, RLS-gated `SELECT`). Event types drive the
UI: `stage` (INTAKE / NORMALIZE / VERDICT), `claim`, `evidence`, `verdict`,
`terminal`. A 5 s backfill poll on reconnect catches events missed during
disconnects. The permalink at `/v/[slug]` is SSR for SEO.

---

## Architecture

### System diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Clients                                                                │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                   │
│  │ Vercel       │  │ WhatsApp     │  │ curl / API   │                   │
│  │ (Next.js 14) │  │ (Twilio)     │  │ consumers    │                   │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘                   │
└─────────┼─────────────────┼─────────────────┼───────────────────────────┘
          │ Realtime        │ webhook         │ REST
          ▼                 ▼                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  Supabase Postgres                                                      │
│  submissions · claims · verdicts · jobs · events_log · embeddings       │
│  pgvector (ivfflat cosine) · RLS on all tables · Realtime on events_log │
└─────────┬───────────────────────────────────────────────┬───────────────┘
          │ direct connection (asyncpg, pool=5)             │ anon SELECT
          ▼                                                 ▼
┌─────────────────────────────┐              ┌────────────────────────────┐
│  Render — juris-web         │              │  Vercel frontend           │
│  FastAPI + in-process worker│              │  investigation + /v/[slug] │
│  ┌─────────┐ ┌───────────┐ │              └────────────────────────────┘
│  │ API     │ │ worker.run│ │
│  │ lifespan│ │ poll loop │ │
│  └────┬────┘ └─────┬─────┘ │
│       │            │       │
│       └─────┬──────┘       │
│             ▼              │
│  pipeline/ + services/     │
└──────┬──────────┬──────────┘
       │          │
       ▼          ▼
┌────────────┐  ┌────────────────┐
│ MeshAPI    │  │ Render SearXNG │
│ (all LLMs) │  │ (meta-search)  │
└────────────┘  └───────┬────────┘
                        │
                        ▼
                 ┌──────────────────┐
                 │ Trafilatura      │
                 │ (page fetch,     │
                 │  falls back to   │
                 │  Jina Reader)    │
                 └──────────────────┘
```

### Layer breakdown

| Layer | Tech | Where | Responsibility |
|---|---|---|---|
| Frontend | Next.js 14 (App Router), Tailwind, `@supabase/supabase-js` Realtime | Vercel | Intake form, live investigation feed, SSR verdict permalinks |
| Backend API | FastAPI (async), CORS open | Render web service | `POST /api/verify`, `GET /api/jobs/{id}/events`, `GET /api/verdicts/{slug}`, `POST /webhooks/whatsapp` |
| Worker | asyncio poll loop in same process (`lifespan` task) | Render web service | `claim_next()` → `orchestrator.run()` → `mark_done` / `mark_error` |
| Meta-search | SearXNG (custom Docker image — JSON API enabled, limiter off) | Render web service | Aggregates web/news results; retried on 502/503/504 cold starts |
| Page fetch | Trafilatura (in-process HTML parse), falls back to Jina Reader (`r.jina.ai`) for thin/empty results | In-process / External | Full-text extraction for top search hits |
| Database | Postgres + pgvector, Realtime publication | Supabase | Persistence, job queue, live event stream |
| Models | MeshAPI (OpenAI-compatible router) | `api.meshapi.ai` | Normalizer, verifier, synthesizer, OCR — unified billing |
| Job queue | Postgres `jobs` table, `FOR UPDATE SKIP LOCKED` | Supabase | Postgres-backed queue; safe under concurrent workers |
| Tracing | LangSmith (`@traceable` on pipeline stages) | LangSmith cloud | Optional; flush on job completion |
| Integrations | Twilio (WhatsApp) | Various | Inbound claims + outbound verdict delivery |

### Data flow through services

| Module | Role |
|---|---|
| `pipeline/s0_intake.py` | Content-type resolution → text |
| `pipeline/s1_normalize.py` | Language detect + claim decomposition |
| `pipeline/verify.py` | Tool-calling verifier loop + code-enforced guards |
| `pipeline/synthesize.py` | AND-combine, format/summarize, persist `VerdictCard` |
| `pipeline/orchestrator.py` | Stage machine, parallel verify, WhatsApp delivery |
| `services/mesh.py` | MeshAPI client, rate limiting (20 req/min), Pydantic schema validation |
| `services/search.py` | SearXNG wrapper, page fetch (Trafilatura → Jina fallback), temporal heuristics, evidence shaping |
| `services/tools.py` | Tool registry (`search`) |
| `services/jobs.py` | Enqueue / claim / status transitions |
| `services/events.py` | Append-only `events_log` writes |
| `services/whatsapp.py` | Twilio send helpers for text + verdict cards |
| `services/credibility.py` | Domain credibility table (`data/domains.yaml`) |
| `worker.py` | 2 s poll interval; isolated exception handling per job |

### Role → model matrix (`backend/app/config.yaml`)

All model calls go through MeshAPI, a single OpenAI-compatible router with
unified billing across providers.

| Role | Model | Notes |
|---|---|---|
| Normalizer | `openai/gpt-oss-120b` | Structured JSON extraction, temp 0.0 |
| Verifier | `openai/gpt-oss-120b` | Tool-calling + structured verdict, temp 0.1 |
| Synthesizer | `openai/gpt-oss-120b` | Multi-claim summary only, temp 0.2 |
| OCR | `google/gemma-3-4b-it` | Vision input for image intake, temp 0.0 |

Exact IDs are read from `config.yaml` at runtime. Every model call validates
against a Pydantic schema; one retry on invalid output, then degrade/fallback.

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
      s0_intake.py         request parsing, content-type detection
      s1_normalize.py      language detection, claim extraction
      verify.py            iterative tool-calling verifier loop
      synthesize.py        verdict card builder + permalink generation
      orchestrator.py      pipeline orchestration
    services/              mesh.py (MeshAPI client), search.py, tools.py, credibility.py, citations.py, jobs.py, events.py, whatsapp.py
    data/                  domains.yaml (credibility table), factcheckers.yaml (IFCN sites)
  migrations/              0001 schema, 0002 RLS, 0003 frontend/Realtime, 0004 WhatsApp, 0005 QA evidence, 0006 verdict, 0007 drop trials
  tests/                   offline (mocked) + @needs_db live tests
frontend/
  app/
    page.tsx               intake
    investigation/[id]/page.tsx    live investigation feed (Supabase Realtime)
    v/[slug]/page.tsx      SSR verdict permalink (SEO-crawlable)
  components/               StageRail, EvidenceCard, VerdictCardView, RebuttalCard, etc.
  lib/                      types, Supabase client, API config, citation renderer
searxng/                   custom Dockerfile + settings.yml (JSON API, limiter off)
design/                    phase-by-phase spec docs (design/phase-N-*.md) + HLD/LLD
render.yaml                Render Blueprint (web + searxng services)
docker-compose.yml         Local dev: Postgres, Supabase emulator (optional)
```

---

## Environment variables

**Backend** (`.env` at repo root, loaded by `app/config.py` and `tests/conftest.py`):

| Var | Purpose |
|---|---|
| `DATABASE_URL` | Supabase Postgres connection (session pooler; pool capped at 5 to stay under the free-tier 15-client limit) |
| `MESH_API_KEY` | MeshAPI key (sole model provider for normalizer, verifier, synthesizer, and OCR) |
| `SEARXNG_URL` | URL of the SearXNG meta-search service (`search` tool) |
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
  subscribes to Supabase Realtime with a 5s backfill poll for reconnects.
- **Database** — Supabase Postgres. Migrations in `backend/migrations/`,
  applied directly (no migration-runner service). RLS is on for every table;
  the backend uses the direct Postgres connection (bypasses RLS), while the
  frontend gets anon `SELECT` policies on `events_log`/`verdicts` only.

### GitHub Actions workflows (`.github/workflows/`)

Both are cron-triggered keep-alive pings — Render's free tier spins services
down after ~15 min idle and Supabase auto-pauses a project after 7 days idle.

| Workflow | Schedule | Purpose |
|---|---|---|
| `health-cron.yml` | daily, 06:00 UTC | Curls `juris-web`'s `/health`; keeps Supabase from auto-pausing and warms the Render web service. Reads the `HEALTH_URL` repo secret. |
| `searxng-warm.yml` | every 14 min | Curls SearXNG with a throwaway query, retrying up to 4× on cold-start 502s. Reads the optional `SEARXNG_PING_URL` repo secret (defaults to the public SearXNG URL). |

Both also support `workflow_dispatch` for a manual run from the Actions tab.

---

## Research foundations

The single-verifier pipeline is grounded in published work on retrieval-augmented
fact-checking, iterative retrieval, and temporal validity. Key references:

| Paper | What we took from it |
|---|---|
| [Loki / OpenFactVerification](https://arxiv.org/abs/2410.01794) | Simple linear pipeline, explicit check-worthiness filtering, and query reformulation around the underlying fact instead of the claim's wording |
| [FIRE: Fact-checking with Iterative Retrieval and Verification](https://arxiv.org/abs/2411.00784) | Single agent deciding whether to answer now or search again; the core shape of the Verify loop |
| [AVeriTeC shared task 2025](https://aclanthology.org/2025.fever-1.15/) | Evidence that simple single-agent / RAG fact-checking pipelines work well at scale |
| [Temporal failure modes in statutory QA](https://arxiv.org/abs/2605.23497) | Hard temporal filtering by as-of date matters more than prompt hints for time-sensitive claims |
| [SemanticCite](https://arxiv.org/abs/2511.16198) | Verdicts should be grounded in fetched page content, not snippets alone |
| [OpenFactCheck](https://arxiv.org/abs/2408.11832) | Retrieval silence on static claims is weak evidence, supporting the constrained parametric fallback for non-time-sensitive facts |
