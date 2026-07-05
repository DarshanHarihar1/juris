# Juris — Implementation Plan

Phased build plan derived from `../HLD.md` and `../LLD.md`. **v1 scope: text-only input, text VerdictCard, all models via NVIDIA NIM free tier.**

Each phase is a vertical slice with its own goal, deliverables, milestones, and testing criteria. Phases 0→5 are the backend pipeline (sequential-ish); Phase 6 (frontend) runs in parallel from Phase 1 onward; Phase 7 is integration + demo.

## Phase index

| # | Phase | Covers | Owner track | LLD day |
|---|---|---|---|---|
| 0 | [Foundation & Infra](phase-0-foundation.md) | Supabase schema, Render services, NIM client, config matrix, job queue, events plumbing | Backend | 1 |
| 1 | [Intake & Normalize](phase-1-intake-normalize.md) | REST API, S0 text intake, S1 normalizer, orchestrator skeleton | Backend | 1 |
| 2 | [Precedent & Search Tools](phase-2-precedent-search.md) | S2 cache + Google FactCheck + SearXNG, tool framework, credibility scorer | Backend | 2–3 |
| 3 | [Investigation](phase-3-investigation.md) | S3 tool-using investigator agents (2), evidence log | Backend | 2 |
| 4 | [Verdict Engine](phase-4-verdict-engine.md) | S4 fast-path jury + agreement, S5 trial, citation validator | Backend | 3–4 |
| 5 | [Synthesis & Output](phase-5-synthesis-output.md) | S6 verdict card (text), rebuttal, manipulation tags, permalink | Backend | 5 |
| 6 | [Live Courtroom UI](phase-6-frontend.md) | Next.js PWA, Supabase Realtime stream, courtroom view, permalink page | Frontend | 2–5 |
| 7 | [WhatsApp, Eval & Demo](phase-7-whatsapp-eval-demo.md) | Twilio WhatsApp adapter, golden-claim eval, `/stats`, demo video | Both | 6–7 |

## Definition of "done" for a phase
A phase is done when: (1) all milestones checked, (2) every item in **Testing criteria** passes, (3) it's deployed/running on the target infra (not just localhost), (4) committed in small commits (hackathon anti-dump rule).

## Global conventions
- **Models:** all via NVIDIA NIM (`https://integrate.api.nvidia.com/v1`, OpenAI-compatible, `Bearer $NVIDIA_API_KEY`). Role→model matrix in `backend/app/config.yaml` (LLD §2).
- **Rate limit:** ~40 req/min free tier → global semaphore in `services/nim.py`. Treat 429s as expected.
- **DB:** Supabase Postgres + pgvector. Job queue = `jobs` table (`FOR UPDATE SKIP LOCKED`). Live events = `events_log` → Supabase Realtime. No Redis.
- **Structured output everywhere:** every model call validates against a Pydantic schema; retry ×1 on invalid, then degrade/fallback.
- **Cost:** $0 (NIM free tier). The budget we manage is requests/min, not dollars.
