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
      const { investigation_url, job_id } = await r.json();
      router.push(investigation_url || `/investigation/${job_id}`);
    } catch (e: any) {
      setErr(e?.message ?? "Something went wrong. Try again.");
      setLoading(false);
    }
  }

  return (
    <main className="flex min-h-dvh flex-col">
      <header className="px-6 py-5">
        <Wordmark />
      </header>

      <div className="flex flex-1 items-center justify-center px-6">
        <div className="-mt-16 w-full max-w-xl">
          <h1 className="font-serif text-4xl font-semibold leading-tight tracking-tight sm:text-5xl">
            rumor ends here<span className="text-verdict-false">.</span>
          </h1>
          <p className="mt-3 text-muted">
            don&rsquo;t forward it. verify it.
          </p>

          <div
            className="mt-8 rounded-2xl border border-line bg-white/70 shadow-sm
                       transition focus-within:border-ink/30 focus-within:bg-white"
          >
            <div className="flex gap-1 border-b border-line/60 px-3 pt-3 pb-2 text-sm">
              {(["text", "url", "image"] as Mode[]).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={`rounded-full px-3 py-1 capitalize transition ${
                    mode === m ? "bg-ink text-paper" : "text-muted hover:text-ink"
                  }`}
                >
                  {m}
                </button>
              ))}
            </div>

            {mode === "image" ? (
              <label
                className="flex h-28 w-full cursor-pointer flex-col items-center justify-center
                           gap-1 text-sm text-muted transition hover:text-ink"
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
                  // Enter submits; Shift+Enter inserts a newline.
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    submit();
                  }
                }}
                placeholder={
                  mode === "url"
                    ? "paste a link to an article…"
                    : "paste the message everyone's forwarding…"
                }
                rows={3}
                autoFocus
                className="w-full resize-none bg-transparent px-4 py-3 text-[15px]
                           leading-relaxed outline-none placeholder:text-muted/60"
              />
            )}

            <div className="flex items-center justify-end px-3 pb-3">
              <button
                onClick={submit}
                disabled={loading || !content}
                className="shrink-0 rounded-full bg-ink px-5 py-2 text-sm font-medium text-paper
                           transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {loading ? "Filing…" : "Verify"}
              </button>
            </div>
          </div>

          {mode === "text" && (
            <div className="mt-4 flex flex-wrap gap-2">
              {SAMPLES.map((s, i) => (
                <button
                  key={i}
                  onClick={() => setText(s)}
                  className="max-w-[14rem] truncate rounded-full border border-line px-3 py-1
                             text-xs text-muted transition hover:border-ink/30 hover:text-ink
                             sm:max-w-[18rem]"
                  title={s}
                >
                  {s}
                </button>
              ))}
            </div>
          )}

          {err && <p className="mt-3 text-sm text-verdict-false">{err}</p>}
        </div>
      </div>
    </main>
  );
}
