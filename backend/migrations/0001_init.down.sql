-- Juris — Phase 0 schema teardown (for migrate-down/up round-trip test).
drop table if exists jobs cascade;
drop table if exists events_log cascade;
drop table if exists verdicts cascade;
drop table if exists trials cascade;
drop table if exists evidence cascade;
drop table if exists claims cascade;
drop table if exists submissions cascade;
