-- Juris — Phase 0 schema (LLD §4.6). 7 tables + pgvector + ivfflat.
-- Idempotent: safe to re-run.

create extension if not exists vector;

-- 1. submissions
create table if not exists submissions (
    id            uuid primary key default gen_random_uuid(),
    channel       text not null check (channel in ('whatsapp', 'web')),
    user_hash     text not null,
    media_type    text not null check (media_type in ('text', 'image', 'audio', 'url')),
    raw_text      text,
    media_uri     text,
    detected_lang text,
    created_at    timestamptz not null default now()
);

-- 2. claims (embedding vector(1024) + ivfflat index)
create table if not exists claims (
    id               uuid primary key default gen_random_uuid(),
    submission_id    uuid not null references submissions(id) on delete cascade,
    text_original    text not null,
    text_norm        text not null,
    text_norm_native text not null,
    claim_type       text not null,
    embedding        vector(1024),
    created_at       timestamptz not null default now()
);
create index if not exists claims_embedding_ivfflat
    on claims using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- 3. evidence
create table if not exists evidence (
    id           uuid primary key default gen_random_uuid(),
    claim_id     uuid not null references claims(id) on delete cascade,
    url          text not null,
    domain       text not null,
    title        text,
    snippet      text,
    published_at date,
    stance       text not null check (stance in ('supports', 'refutes', 'mentions', 'context')),
    credibility  real,
    found_by     text,
    created_at   timestamptz not null default now()
);

-- 4. trials (transcript + ruling as jsonb)
create table if not exists trials (
    id         uuid primary key default gen_random_uuid(),
    claim_id   uuid not null references claims(id) on delete cascade,
    transcript jsonb not null default '[]'::jsonb,
    ruling     jsonb,
    created_at timestamptz not null default now()
);

-- 5. verdicts (cache = claims JOIN verdicts where confidence >= 70)
create table if not exists verdicts (
    id         uuid primary key default gen_random_uuid(),
    claim_id   uuid not null references claims(id) on delete cascade,
    slug       text unique not null,
    verdict    text not null check (verdict in ('TRUE','FALSE','MISLEADING','UNVERIFIABLE','CONFLICTING')),
    confidence int not null,
    card       jsonb not null,
    path       text check (path in ('cache','precedent','consensus','trial')),
    created_at timestamptz not null default now()
);

-- 6. events_log (drives live courtroom via Supabase Realtime)
create table if not exists events_log (
    id         bigserial primary key,
    job_id     uuid not null,
    event      text not null,
    data       jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);
create index if not exists events_log_job_id on events_log (job_id, id);

-- 7. jobs (queue via SELECT ... FOR UPDATE SKIP LOCKED)
create table if not exists jobs (
    id            uuid primary key default gen_random_uuid(),
    submission_id uuid references submissions(id) on delete cascade,
    status        text not null default 'queued' check (status in ('queued','running','done','error')),
    attempts      int not null default 0,
    payload       jsonb not null default '{}'::jsonb,
    last_error    text,
    claimed_at    timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create index if not exists jobs_queued on jobs (created_at) where status = 'queued';
