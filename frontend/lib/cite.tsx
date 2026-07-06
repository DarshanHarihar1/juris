import React from "react";

// Render [e:eN] citation tags as small superscript chips instead of raw text.
export function withCites(text: string): React.ReactNode {
  const re = /\[e:([^\]]+)\]/g;
  const out: React.ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(
      <sup key={i++} className="ml-0.5 font-mono text-[10px] text-muted">
        [{m[1]}]
      </sup>
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}
