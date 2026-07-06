"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { API_URL } from "@/lib/config";
import { Wordmark } from "@/components/Wordmark";

const SAMPLES = [
  "Drinking hot lemon water at 4am cures cancer. Forward to 10 people!",
  "The Great Wall of China is the only man-made object visible from space.",
];

export default function Home() {
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const router = useRouter();

  async function submit() {
    const content = text.trim();
    if (!content || loading) return;
    setLoading(true);
    setErr(null);
    try {
      const r = await fetch(`${API_URL}/api/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: "text", content }),
      });
      if (!r.ok) throw new Error(`Server responded ${r.status}`);
      const { job_id } = await r.json();
      router.push(`/trial/${job_id}`);
    } catch (e: any) {
      setErr(e?.message ?? "Something went wrong. Try again.");
      setLoading(false);
    }
  }

  return (
    <main className="min-h-dvh flex flex-col">
      <header className="px-6 py-5">
        <Wordmark />
      </header>

      <div className="flex-1 flex items-center justify-center px-6">
        <div className="w-full max-w-xl -mt-16">
          <h1 className="text-3xl sm:text-4xl font-semibold tracking-tight leading-tight">
            Is it true?
          </h1>
          <p className="mt-3 text-muted leading-relaxed">
            Paste a claim or a forwarded message. Two investigators gather evidence, a jury
            votes, and — if they disagree — it goes to trial. You get a cited verdict.
          </p>

          <div className="mt-6">
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit();
              }}
              placeholder="Paste the claim here…"
              rows={4}
              className="w-full resize-none rounded-xl border border-line bg-white/60 px-4 py-3
                         text-[15px] leading-relaxed outline-none transition
                         focus:border-ink/30 focus:bg-white placeholder:text-muted/60"
            />

            <div className="mt-3 flex items-center justify-between gap-4">
              <div className="flex flex-wrap gap-2">
                {SAMPLES.map((s, i) => (
                  <button
                    key={i}
                    onClick={() => setText(s)}
                    className="text-xs text-muted hover:text-ink border border-line hover:border-ink/30
                               rounded-full px-3 py-1 transition truncate max-w-[10rem] sm:max-w-[14rem]"
                    title={s}
                  >
                    {s}
                  </button>
                ))}
              </div>

              <button
                onClick={submit}
                disabled={loading || !text.trim()}
                className="shrink-0 rounded-full bg-ink text-paper text-sm font-medium px-5 py-2.5
                           transition hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {loading ? "Filing…" : "Verify"}
              </button>
            </div>

            {err && <p className="mt-3 text-sm text-verdict-false">{err}</p>}
          </div>

          <p className="mt-8 text-xs text-muted/70 font-mono">
            v1 · text only · powered by NVIDIA NIM
          </p>
        </div>
      </div>
    </main>
  );
}
