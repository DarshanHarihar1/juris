-- Phase 6 frontend: anon read access for the live courtroom + permalinks.
-- The live trial page subscribes to events_log via Supabase Realtime and back-fills
-- missed events with a select; both need an anon SELECT policy. Verdicts are public.
-- (The backend keeps using the direct postgres connection, which bypasses RLS.)

drop policy if exists "anon read events_log" on events_log;
create policy "anon read events_log" on events_log for select using (true);

drop policy if exists "anon read verdicts" on verdicts;
create policy "anon read verdicts" on verdicts for select using (true);

-- Realtime broadcasts Postgres changes only for tables in this publication.
do $$ begin
  alter publication supabase_realtime add table events_log;
exception when duplicate_object then null;
end $$;
