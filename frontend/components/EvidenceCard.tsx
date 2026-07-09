const STANCE_COLOR: Record<string, string> = {
  supports: "text-verdict-true",
  refutes: "text-verdict-false",
  mentions: "text-muted",
  context: "text-verdict-misleading",
};

export function EvidenceCard({ ev }: { ev: Record<string, any> }) {
  const stance = String(ev.stance ?? "");
  const cred = Math.max(0, Math.min(1, Number(ev.credibility) || 0));
  return (
    <a
      href={ev.url}
      target="_blank"
      rel="noopener noreferrer"
      className="block rounded-lg border border-line bg-white px-4 py-3 transition
                 hover:border-ink/20 animate-fade-up"
    >
      <div className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-2 truncate text-sm font-medium">
          {ev.domain}
          {/* credibility as a 5-dot meter instead of a raw number */}
          <span className="flex gap-0.5" title={`credibility ${Math.round(cred * 100)}%`}>
            {[0, 1, 2, 3, 4].map((i) => (
              <span
                key={i}
                className={`h-1 w-1 rounded-full ${cred * 5 > i ? "bg-ink/60" : "bg-line"}`}
              />
            ))}
          </span>
        </span>
        <span className={`font-mono text-[11px] uppercase tracking-wide ${STANCE_COLOR[stance] ?? "text-muted"}`}>
          {stance}
        </span>
      </div>
      {ev.title && <p className="mt-1 line-clamp-2 text-sm text-ink/80">{ev.title}</p>}
      {ev.snippet && <p className="mt-1 line-clamp-2 text-xs text-muted">{ev.snippet}</p>}
    </a>
  );
}
