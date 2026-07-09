"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { API_URL } from "@/lib/config";
import { Wordmark } from "@/components/Wordmark";

const SAMPLES = [
  "Drinking hot lemon water at 4am cures cancer. Forward to 10 people!",
  "The Great Wall of China is the only man-made object visible from space.",
];

type Mode = "text" | "url" | "image";

export default function Home() {
  const [mode, setMode] = useState<Mode>("text");
  const [text, setText] = useState("");
  const [image, setImage] = useState<{ name: string; data: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const router = useRouter();

  // content = the trimmed claim/url, or the image data URL for OCR.
  const content = mode === "image" ? image?.data ?? "" : text.trim();

  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => setImage({ name: f.name, data: reader.result as string });
    reader.readAsDataURL(f); // data:image/...;base64,... — S0 OCRs it
  }

  async function submit() {
    if (!content || loading) return;
    setLoading(true);
    setErr(null);
    try {
      const r = await fetch(`${API_URL}/api/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: mode, content }),
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
            <div className="mb-3 flex gap-1 text-sm">
              {(["text", "url", "image"] as Mode[]).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={`rounded-full px-3 py-1 capitalize transition ${
                    mode === m
                      ? "bg-ink text-paper"
                      : "text-muted hover:text-ink border border-line"
                  }`}
                >
                  {m}
                </button>
              ))}
            </div>

            {mode === "image" ? (
              <label
                className="flex h-28 w-full cursor-pointer flex-col items-center justify-center gap-1
                           rounded-xl border border-dashed border-line bg-white/60 text-sm text-muted
                           transition hover:border-ink/30 hover:bg-white"
              >
                <input type="file" accept="image/*" onChange={onFile} className="hidden" />
                {image ? (
                  <span className="text-ink">{image.name}</span>
                ) : (
                  <span>Click to upload a screenshot — its text is read via OCR</span>
                )}
              </label>
            ) : (
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit();
                }}
                placeholder={mode === "url" ? "Paste a link to an article…" : "Paste the claim here…"}
                rows={4}
                className="w-full resize-none rounded-xl border border-line bg-white/60 px-4 py-3
                           text-[15px] leading-relaxed outline-none transition
                           focus:border-ink/30 focus:bg-white placeholder:text-muted/60"
              />
            )}

            <div className="mt-3 flex items-center justify-between gap-4">
              <div className="flex flex-wrap gap-2">
                {mode === "text" &&
                  SAMPLES.map((s, i) => (
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
                disabled={loading || !content}
                className="shrink-0 rounded-full bg-ink text-paper text-sm font-medium px-5 py-2.5
                           transition hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {loading ? "Filing…" : "Verify"}
              </button>
            </div>

            {err && <p className="mt-3 text-sm text-verdict-false">{err}</p>}
          </div>

        </div>
      </div>
    </main>
  );
}
