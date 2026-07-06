import { withCites } from "@/lib/cite";

// Side 1 = prosecution (left, light), Side 2 = defense (right, ink) — anonymized in the backend.
export function ArgumentBubble({
  side,
  round,
  text,
}: {
  side: string;
  round: number;
  text: string;
}) {
  const pros = side === "Side 1";
  return (
    <div className={`flex ${pros ? "justify-start" : "justify-end"}`}>
      <div
        className={`max-w-[85%] rounded-2xl border px-4 py-3 text-sm animate-fade-up ${
          pros
            ? "rounded-tl-sm border-line bg-white"
            : "rounded-tr-sm border-ink bg-ink text-paper"
        }`}
      >
        <div
          className={`mb-1 font-mono text-[10px] uppercase tracking-wide ${
            pros ? "text-muted" : "text-paper/60"
          }`}
        >
          {pros ? "Prosecution" : "Defense"} · R{round}
        </div>
        <p className="leading-relaxed">{withCites(text)}</p>
      </div>
    </div>
  );
}
