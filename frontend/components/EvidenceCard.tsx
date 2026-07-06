const STANCE_COLOR: Record<string, string> = {
  supports: "text-verdict-true",
  refutes: "text-verdict-false",
  mentions: "text-muted",
  context: "text-verdict-misleading",
};

export function EvidenceCard({ ev }: { ev: Record<string, any> }) {
  const stance = String(ev.stance ?? "");
  return (
    <a
      href={ev.url}
      target="_blank"
      rel="noopener noreferrer"
      className="block rounded-lg border border-line bg-white px-4 py-3 transition
                 hover:border-ink/20 animate-fade-up"
    >
      <div className="flex items-center justify-between gap-3">
        <span className="truncate text-sm font-medium">{ev.domain}</span>
        <span className={`font-mono text-[11px] uppercase tracking-wide ${STANCE_COLOR[stance] ?? "text-muted"}`}>
          {stance}
        </span>
      </div>
      {ev.title && <p className="mt-1 line-clamp-2 text-sm text-ink/80">{ev.title}</p>}
      {ev.snippet && <p className="mt-1 line-clamp-2 text-xs text-muted">{ev.snippet}</p>}
      <div className="mt-2 flex items-center gap-2 font-mono text-[11px] text-muted/70">
        <span>cred {Math.round((Number(ev.credibility) || 0) * 100)}</span>
        {ev.found_by && <span className="truncate">· {String(ev.found_by).split("/").pop()}</span>}
      </div>
    </a>
  );
}
