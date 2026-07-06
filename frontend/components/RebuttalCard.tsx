"use client";

import { useState } from "react";

export function RebuttalCard({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  if (!text) return null;

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard blocked — no-op */
    }
  }

  return (
    <div className="mt-5 rounded-xl border border-line bg-paper p-4">
      <div className="mb-2 flex items-center justify-between">
        <span className="font-mono text-[11px] uppercase tracking-wide text-muted">
          Forward this back
        </span>
        <button
          onClick={copy}
          className="rounded-full border border-line px-2.5 py-0.5 text-[11px] text-muted
                     transition hover:border-ink/30 hover:text-ink"
        >
          {copied ? "Copied ✓" : "Copy"}
        </button>
      </div>
      <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink/85">{text}</p>
    </div>
  );
}
