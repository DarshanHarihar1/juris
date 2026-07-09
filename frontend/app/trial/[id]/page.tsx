"use client";

import { useEffect, useState } from "react";
import { API_URL } from "@/lib/config";
import { EventRow, VerdictCard, STAGES, STAGE_LABEL } from "@/lib/types";
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

const PHASE_LABEL: Record<string, string> = {
  NORMALIZE: "Reading",
  VERIFY: "Verifying",
  VERDICT: "Verdict",
};

function PhaseRail({
  stageStatus,
  finished,
}: {
  stageStatus: Record<string, "started" | "done" | undefined>;
  finished: boolean;
}) {
  return (
    <div className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-wide">
      {STAGES.map((s, i) => {
        const raw = stageStatus[s];
        const st = finished ? "done" : raw;
        return (
          <span key={s} className="flex items-center gap-2">
            {i > 0 && <span className="h-px w-5 bg-line" />}
            <span
              className={`flex items-center gap-1.5 ${
                st === "started"
                  ? "text-ink animate-pulse-soft"
                  : st === "done"
                  ? "text-ink"
                  : "text-muted/40"
              }`}
            >
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  st === "done" ? "bg-ink" : st === "started" ? "bg-ink animate-pulse" : "bg-line"
                }`}
              />
              {PHASE_LABEL[s] ?? STAGE_LABEL[s]}
            </span>
          </span>
        );
      })}
    </div>
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

function Accordion({
  label,
  count,
  children,
  defaultOpen = false,
}: {
  label: string;
  count: string;
  children: React.ReactNode;
  defaultOpen?: boolean;
}) {
  return (
    <details open={defaultOpen} className="group rounded-2xl border border-line bg-white">
      <summary
        className="flex cursor-pointer list-none items-center justify-between px-5 py-4
                   [&::-webkit-details-marker]:hidden"
      >
        <span className="font-mono text-[11px] uppercase tracking-wide text-muted">{label}</span>
        <span className="flex items-center gap-2 font-mono text-[11px] text-muted">
          {count}
          <svg
            viewBox="0 0 12 12"
            className="h-3 w-3 transition-transform group-open:rotate-180"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
          >
            <path d="M2 4.5 6 8.5 10 4.5" />
          </svg>
        </span>
      </summary>
      <div className="space-y-2.5 border-t border-line px-4 py-4">{children}</div>
    </details>
  );
}

export default function TrialPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const [events, setEvents] = useState<EventRow[]>([]);

  // Single SSE subscription. The server replays from Last-Event-ID on reconnect,
  // so no separate backfill/poll paths are needed.
  useEffect(() => {
    const es = new EventSource(`${API_URL}/api/jobs/${id}/stream`);
    const seen = new Set<number>();
    es.onmessage = (m) => {
      const row = JSON.parse(m.data) as EventRow;
      if (seen.has(row.id)) return;
      seen.add(row.id);
      setEvents((prev) => [...prev, { ...row, job_id: id }].sort((a, b) => a.id - b.id));
      // the stream ends after these; close so EventSource doesn't reconnect forever
      if (row.event === "verdict" || row.event === "terminal") es.close();
    };
    return () => es.close();
  }, [id]);

  // derive view state from the ordered event log
  const stageStatus: Record<string, "started" | "done" | undefined> = {};
  const evidence: Record<string, any>[] = [];
  const verifySteps: VerifyStep[] = [];
  let claim: any = null;
  let terminal: any = null;
  let verdict: VerdictCard | null = null;

  for (const e of events) {
    const d = e.data || {};
    switch (e.event) {
      case "stage": {
        const stage = d.stage === "SYNTHESIZE" ? "VERDICT" : d.stage;
        stageStatus[stage] = d.status;
        break;
      }
      case "claim": claim = d; break;
      case "evidence": evidence.push(d); break;
      case "verify_step": verifySteps.push(d); break;
      case "verdict": verdict = d as VerdictCard; break;
      case "terminal": terminal = d; break;
    }
  }

  const finished = verdict !== null || terminal !== null;
  const lastStep = verifySteps[verifySteps.length - 1];
  const activity =
    lastStep?.query
      ? `searching: ${lastStep.query}`
      : lastStep?.thought_summary ??
        (claim ? "weighing the evidence…" : "reading the claim…");

  return (
    <main className="min-h-dvh">
      <header className="border-b border-line px-6 py-4">
        <div className="mx-auto max-w-2xl">
          <Wordmark />
        </div>
      </header>

      <div className="mx-auto max-w-2xl space-y-4 px-6 py-8">
        {/* Hero: live status while running, stamped verdict when done. */}
        {!verdict && (
          <section className="rounded-2xl border border-line bg-white p-6">
            <PhaseRail stageStatus={stageStatus} finished={finished} />

            {claim && (
              <p className="mt-4 font-serif text-2xl font-semibold leading-snug tracking-tight">
                “{claimText(claim)}”
              </p>
            )}

            {!terminal && (
              <>
                <p className="mt-4 font-mono text-xs text-muted animate-pulse-soft">
                  {activity}
                </p>
                <div className="mt-3 h-0.5 overflow-hidden rounded-full bg-line">
                  <div className="h-full w-1/4 rounded-full bg-ink animate-sweep" />
                </div>
              </>
            )}

            {terminal && (
              <p className="mt-4 text-sm text-muted">{terminal.message}</p>
            )}
          </section>
        )}

        {verdict && (
          <section className="animate-fade-up">
            <VerdictCardView card={verdict} />
          </section>
        )}

        {evidence.length > 0 && (
          <Accordion label="Evidence" count={`${evidence.length} source${evidence.length === 1 ? "" : "s"}`}>
            {evidence.map((ev, i) => (
              <EvidenceCard key={ev.id ?? i} ev={ev} />
            ))}
          </Accordion>
        )}

        {verifySteps.length > 0 && (
          <Accordion label="How we checked" count={`${verifySteps.length} step${verifySteps.length === 1 ? "" : "s"}`}>
            {verifySteps.map((entry, i) => (
              <VerifyStepRow key={`${entry.step ?? "step"}-${i}`} entry={entry} />
            ))}
          </Accordion>
        )}
      </div>
    </main>
  );
}
