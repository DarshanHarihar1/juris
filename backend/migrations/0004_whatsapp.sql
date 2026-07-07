-- Phase 7 WhatsApp (LLD §8). Two additions, pipeline schema untouched.
-- Idempotent: safe to re-run.

-- Transient reply address for an in-flight WhatsApp job. Nulled after the verdict is
-- sent (privacy §35 — the raw address never persists past delivery, never hits events_log).
alter table submissions add column if not exists reply_to text;

-- Inbound idempotency: providers (Twilio/Meta) re-POST on webhook timeout. Map the
-- provider message id → the job we already enqueued so a retry returns the same ack
-- instead of re-running the pipeline.
create table if not exists wa_inbound (
    message_sid text primary key,
    job_id      uuid not null references jobs(id) on delete cascade,
    created_at  timestamptz not null default now()
);
