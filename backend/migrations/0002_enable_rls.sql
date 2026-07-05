-- Defense in depth: RLS on every public-schema table (LLD security). Backend uses
-- the direct postgres connection (bypasses RLS). Public-read policies for
-- events_log/verdicts are added in the frontend phase when Realtime/permalinks
-- need anon access.
alter table submissions enable row level security;
alter table claims      enable row level security;
alter table evidence    enable row level security;
alter table trials      enable row level security;
alter table verdicts    enable row level security;
alter table events_log  enable row level security;
alter table jobs        enable row level security;
