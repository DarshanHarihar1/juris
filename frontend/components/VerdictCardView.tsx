import { VerdictCard, VERDICT_COLOR, VERDICT_DOT } from "@/lib/types";
import { withCites } from "@/lib/cite";
import { RebuttalCard } from "./RebuttalCard";

export function VerdictCardView({ card }: { card: VerdictCard }) {
  return (
    <div className="rounded-2xl border border-line bg-white p-6 animate-fade-up">
      <div className="flex flex-wrap items-center gap-2.5">
        <span className={`h-2.5 w-2.5 rounded-full ${VERDICT_DOT[card.verdict]}`} />
        <span className={`font-mono text-sm font-semibold tracking-wide ${VERDICT_COLOR[card.verdict]}`}>
          {card.verdict}
        </span>
        <span className="font-mono text-xs text-muted">
          · {card.confidence}% confidence · via {card.path}
        </span>
      </div>

      <h2 className="mt-3 text-xl font-semibold leading-snug tracking-tight">
        {card.one_liner_native}
      </h2>
      {card.explanation_native && (
        <p className="mt-3 text-[15px] leading-relaxed text-ink/85">
          {withCites(card.explanation_native)}
        </p>
      )}

      {card.manipulation_tags?.length > 0 && (
        <div className="mt-4 flex flex-wrap gap-1.5">
          {card.manipulation_tags.map((t) => (
            <span
              key={t}
              className="rounded-full border border-verdict-misleading/30 bg-verdict-misleading/5
                         px-2 py-0.5 font-mono text-[11px] text-verdict-misleading"
            >
              {t}
            </span>
          ))}
        </div>
      )}

      {card.evidence?.length > 0 && (
        <div className="mt-5 border-t border-line pt-4">
          <div className="mb-2 font-mono text-[11px] uppercase tracking-wide text-muted">
            Sources
          </div>
          <div className="flex flex-col gap-1.5">
            {card.evidence.map((e, i) => (
              <a
                key={i}
                href={e.url}
                target="_blank"
                rel="noopener noreferrer"
                className="truncate text-sm text-ink/80 transition hover:text-ink"
              >
                <span className="mr-2 font-mono text-[11px] text-muted">[e{i + 1}]</span>
                {e.domain}
              </a>
            ))}
          </div>
        </div>
      )}

      <RebuttalCard text={card.rebuttal_card_native} />

      {card.models_used && Object.keys(card.models_used).length > 0 && (
        <div className="mt-4 font-mono text-[10px] leading-relaxed text-muted/60">
          {Object.entries(card.models_used)
            .map(([role, m]) => `${role}: ${String(m).split("/").pop()}`)
            .join("  ·  ")}
        </div>
      )}
    </div>
  );
}
