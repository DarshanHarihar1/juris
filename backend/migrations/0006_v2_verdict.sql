-- 0006: v2 verify-agent verdict/path support.
-- Widen verdict and path checks without disturbing legacy rows.

alter table verdicts drop constraint if exists verdicts_verdict_check;
alter table verdicts add constraint verdicts_verdict_check
    check (verdict in ('TRUE','FALSE','MOSTLY_TRUE','MISLEADING','UNVERIFIABLE','CONFLICTING'));

alter table verdicts drop constraint if exists verdicts_path_check;
alter table verdicts add constraint verdicts_path_check
    check (path in ('cache','precedent','consensus','trial','verify'));
