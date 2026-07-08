-- 0005: QA-decomposition evidence support (Phase 3b rearchitecture).
-- Make stance nullable — QA-mode rows don't carry document-level stance.
-- Add question/answer/answerable for grounded question-answer evidence.

alter table evidence alter column stance drop not null;
alter table evidence drop constraint if exists evidence_stance_check;
alter table evidence add constraint evidence_stance_check
    check (stance is null or stance in ('supports', 'refutes', 'mentions', 'context'));

alter table evidence
    add column if not exists question   text,
    add column if not exists answer     text,
    add column if not exists answerable boolean;
