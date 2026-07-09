"use client";

import { useEffect, useState } from "react";
import { supabase } from "@/lib/supabase";
import { EventRow, VerdictCard } from "@/lib/types";
import { StageRail } from "@/components/StageRail";
import { EvidenceCard } from "@/components/EvidenceCard";
import { VerdictCardView } from "@/components/VerdictCardView";
import { Wordmark } from "@/components/Wordmark";

type VerifyStep = {
  step?: number;
  thought_summary?: string;
  query?: string;
  settled?: boolean;
};

function textValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function claimText(claim: Record<string, any>): string {
  return (
    textValue(claim.text_norm_native) ??
    textValue(claim.text_norm) ??
    textValue(claim.text) ??
    textValue(claim.claim) ??
    "Claim received"
  );
}

function VerifyStepRow({ entry }: { entry: VerifyStep }) {
  const settled = entry.settled === true;
  return (
    <div className="animate-fade-up rounded-lg border border-line bg-white px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="font-mono text-[11px] uppercase tracking-wide text-muted">
          Step {entry.step ?? "?"}
        </div>
        <span
          className={`rounded-full px-2 py-0.5 font-mono text-[10px] uppercase tracking-wide ${
            settled ? "bg-verdict-true/10 text-verdict-true" : "bg-paper text-muted"
          }`}
        >
          {settled ? "Settled" : "Searching"}
        </span>
      </div>
      {entry.thought_summary && (
        <p className="mt-2 text-sm leading-relaxed text-ink/85">{entry.thought_summary}</p>
      )}
      {entry.query && (
        <div className="mt-2 rounded-md bg-paper px-3 py-2 font-mono text-xs text-muted">
          {entry.query}
        </div>
      )}
    </div>
  );
}

export default function TrialPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const [events, setEvents] = useState<EventRow[]>([]);

  useEffect(() => {
    let active = true;
    const seen = new Set<number>();
    const add = (rows: EventRow[]) => {
      const fresh = rows.filter((r) => r && !seen.has(r.id));
      if (fresh.length === 0) return;
      fresh.forEach((r) => seen.add(r.id));
      setEvents((prev) => [...prev, ...fresh].sort((a, b) => a.id - b.id));
    };
    const backfill = () =>
      supabase
        .from("events_log")
        .select("*")
        .eq("job_id", id)
        .order("id")
        .then(({ data }) => active && data && add(data as EventRow[]));

    backfill(); // catch anything before the subscription attached
    const channel = supabase
      .channel(`job:${id}`)
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "events_log", filter: `job_id=eq.${id}` },
        (payload) => add([payload.new as EventRow])
      )
      .subscribe();
    // safety re-poll: back-fills any event missed across a reconnect
    const poll = setInterval(backfill, 5000);

    return () => {
      active = false;
      clearInterval(poll);
      supabase.removeChannel(channel);
    };
  }, [id]);

  // derive view state from the ordered event log
  const stageStatus: Record<string, "started" | "done"> = {};
  const evidence: Record<string, any>[] = [];
  const verifySteps: VerifyStep[] = [];
  let claim: any = null;
  let terminal: any = null;
  let verdict: VerdictCard | null = null;

  for (const e of events) {
    const d = e.data || {};
    switch (e.event) {
      case "stage":
        stageStatus[d.stage] = d.status;
        break;
      case "claim": claim = d; break;
      case "evidence": evidence.push(d); break;
      case "verify_step": verifySteps.push(d); break;
      case "verdict": verdict = d as VerdictCard; break;
      case "terminal": terminal = d; break;
    }
  }

  const started = events.length > 0;
  const finished = verdict !== null || terminal !== null;

  return (
    <main className="min-h-dvh">
      <header className="sticky top-0 z-10 border-b border-line bg-paper/80 px-6 py-3 backdrop-blur">
        <div className="mx-auto flex max-w-2xl items-center justify-between gap-4">
          <Wordmark />
          <div className="hidden sm:block">
            <StageRail stageStatus={stageStatus} finished={finished} />
          </div>
        </div>
        <div className="mt-2 overflow-x-auto sm:hidden">
          <StageRail stageStatus={stageStatus} finished={finished} />
        </div>
      </header>

      <div className="mx-auto max-w-2xl space-y-8 px-6 py-8">
        {!started && (
          <div className="animate-pulse-soft py-20 text-center text-muted">
            <p className="font-mono text-sm">starting the investigation...</p>
            <p className="mt-2 text-xs text-muted/60">the free instance may cold-start (~30s)</p>
          </div>
        )}

        {claim && (
          <section className="animate-fade-up rounded-2xl border border-line bg-white p-5">
            <div className="font-mono text-[11px] uppercase tracking-wide text-muted">Claim</div>
            <p className="mt-2 text-xl font-semibold leading-snug tracking-tight">
              {claimText(claim)}
            </p>
            {(claim.volatility || claim.as_of_date) && (
              <div className="mt-3 flex flex-wrap gap-2 font-mono text-[11px] uppercase tracking-wide text-muted">
                {claim.volatility && (
                  <span className="rounded-full border border-line px-2.5 py-1">
                    {claim.volatility}
                  </span>
                )}
                {claim.as_of_date && (
                  <span className="rounded-full border border-line px-2.5 py-1">
                    as of {claim.as_of_date}
                  </span>
                )}
              </div>
            )}
          </section>
        )}

        {terminal && (
          <div className="animate-fade-up rounded-xl border border-line bg-white p-5 text-sm text-muted">
            {terminal.message}
          </div>
        )}

        {evidence.length > 0 && (
          <section>
            <div className="mb-3 font-mono text-[11px] uppercase tracking-wide text-muted">
              Evidence · {evidence.length}
            </div>
            <div className="space-y-2.5">
              {evidence.map((ev, i) => (
                <EvidenceCard key={ev.id ?? i} ev={ev} />
              ))}
            </div>
          </section>
        )}

        {verifySteps.length > 0 && (
          <section>
            <div className="mb-3 font-mono text-[11px] uppercase tracking-wide text-muted">
              Verify · {verifySteps.length}
            </div>
            <div className="space-y-2.5">
              {verifySteps.map((entry, i) => (
                <VerifyStepRow key={`${entry.step ?? "step"}-${i}`} entry={entry} />
              ))}
            </div>
          </section>
        )}

        {verdict && (
          <section>
            <VerdictCardView card={verdict} />
          </section>
        )}
      </div>
    </main>
  );
}
