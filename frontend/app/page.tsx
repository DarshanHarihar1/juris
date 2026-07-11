import Link from "next/link";
import { Wordmark } from "@/components/Wordmark";

export const metadata = {
  title: "Juris — rumor ends here",
  description:
    "Paste any forward. Juris verifies it live — searching, weighing evidence — and returns a cited verdict.",
};

const STEPS = [
  {
    n: "01",
    title: "Paste the forward",
    body: "A message, a link, or a screenshot. Whatever landed in the group chat.",
  },
  {
    n: "02",
    title: "Watch it get checked",
    body: "Juris searches, reads sources, and weighs the evidence live — no black box.",
  },
  {
    n: "03",
    title: "Get a cited verdict",
    body: "One clear ruling, every claim traced back to a source you can open yourself.",
  },
];

export default function Landing() {
  return (
    <main className="flex h-dvh flex-col overflow-hidden">
      <header className="px-6 py-5">
        <Wordmark />
      </header>

      {/* Hero — the single CTA lives here */}
      <section className="flex flex-1 items-center px-6">
        <div className="mx-auto w-full max-w-2xl text-center">
          <p className="font-mono text-xs uppercase tracking-[0.2em] text-muted">
            fact-checking, in the open
          </p>
          <h1 className="mt-5 font-serif text-5xl font-semibold leading-[1.05] tracking-tight sm:text-6xl">
            rumor ends here<span className="text-verdict-false">.</span>
          </h1>
          <p className="mx-auto mt-5 max-w-lg text-lg leading-relaxed text-muted">
            That message everyone&rsquo;s forwarding? Juris checks it live,
            weighs the evidence, and hands back a verdict you can cite.
          </p>
          <div className="mt-9">
            <Link
              href="/verify"
              className="inline-block rounded-full bg-ink px-6 py-2.5 text-sm font-medium text-paper transition hover:opacity-90"
            >
              Verify a claim
            </Link>
          </div>
        </div>
      </section>

      {/* Steps band */}
      <section className="border-t border-line px-6 py-10">
        <div className="mx-auto max-w-3xl">
          <h2 className="font-serif text-2xl font-semibold tracking-tight">
            Nothing hidden. Everything cited.
          </h2>
          <div className="mt-8 grid gap-8 sm:grid-cols-3">
            {STEPS.map((s) => (
              <div key={s.n}>
                <span className="font-mono text-xs text-muted">{s.n}</span>
                <h3 className="mt-3 font-serif text-lg font-semibold">
                  {s.title}
                </h3>
                <p className="mt-2 text-sm leading-relaxed text-muted">
                  {s.body}
                </p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <footer className="border-t border-line px-6 py-5">
        <div className="mx-auto max-w-3xl text-center font-mono text-xs text-muted">
          rumor ends here.
        </div>
      </footer>
    </main>
  );
}
