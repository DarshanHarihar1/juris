import { VerdictCard, VERDICT_COLOR, VERDICT_BORDER } from "@/lib/types";
import { withCites } from "@/lib/cite";
import { RebuttalCard } from "./RebuttalCard";

export function VerdictCardView({ card }: { card: VerdictCard }) {
  return (
    <div className="rounded-2xl border border-line bg-white p-6 animate-fade-up">
      <div className="flex flex-wrap items-baseline gap-4">
        {/* rubber-stamp verdict mark — the headline itself */}
        <span
          className={`inline-block rounded-md border-[3px] px-4 py-1.5 font-mono text-2xl
                      font-bold uppercase tracking-widest animate-stamp
                      ${VERDICT_BORDER[card.verdict]} ${VERDICT_COLOR[card.verdict]}`}
        >
          {card.verdict.replace(/_/g, " ")}
        </span>
        <span className="font-mono text-xs text-muted">{card.confidence}% confidence</span>
      </div>
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
    </div>
  );
}
