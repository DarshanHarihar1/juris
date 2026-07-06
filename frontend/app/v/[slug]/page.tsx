import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { API_URL } from "@/lib/config";
import { VerdictCard } from "@/lib/types";
import { VerdictCardView } from "@/components/VerdictCardView";
import { Wordmark } from "@/components/Wordmark";

// SSR so the permalink is SEO-crawlable (view-source shows the verdict text).
async function getCard(slug: string): Promise<VerdictCard | null> {
  try {
    const r = await fetch(`${API_URL}/api/verdicts/${slug}`, { next: { revalidate: 300 } });
    if (!r.ok) return null;
    return (await r.json()) as VerdictCard;
  } catch {
    return null;
  }
}

export async function generateMetadata({
  params,
}: {
  params: { slug: string };
}): Promise<Metadata> {
  const card = await getCard(params.slug);
  if (!card) return { title: "Verdict not found — Juris" };
  return {
    title: `${card.verdict}: ${card.one_liner_native} — Juris`,
    description: (card.explanation_native || "").slice(0, 160),
  };
}

export default async function PermalinkPage({ params }: { params: { slug: string } }) {
  const card = await getCard(params.slug);
  if (!card) notFound();

  return (
    <main className="min-h-dvh">
      <header className="px-6 py-5">
        <Wordmark />
      </header>
      <div className="mx-auto max-w-2xl px-6 py-4">
        <VerdictCardView card={card} />
        <p className="mt-6 text-center font-mono text-xs text-muted/70">
          Verified by Juris ·{" "}
          <a href="/" className="transition hover:text-ink">
            check another claim →
          </a>
        </p>
      </div>
    </main>
  );
}
