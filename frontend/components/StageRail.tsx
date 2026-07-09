import { STAGES, STAGE_LABEL } from "@/lib/types";

type Status = "started" | "done" | undefined;

export function StageRail({
  stageStatus,
  finished,
}: {
  stageStatus: Record<string, Status>;
  finished?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5 font-mono text-[11px]">
      {STAGES.map((s) => {
        // Once a verdict lands, never leave a stage blinking if its done event was missed.
        const raw = stageStatus[s];
        const st: Status = finished && raw === "started" ? "done" : raw;
        const dot =
          st === "done" ? "bg-ink" : st === "started" ? "bg-ink animate-pulse" : "bg-line";
        const chip =
          st === "done"
            ? "border-ink/15 text-ink bg-white"
            : st === "started"
            ? "border-ink/30 text-ink bg-white animate-pulse-soft"
            : "border-line text-muted/50";
        return (
          <span
            key={s}
            className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 transition ${chip}`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
            {STAGE_LABEL[s]}
          </span>
        );
      })}
    </div>
  );
}
