import Link from "next/link";

export function Wordmark() {
  return (
    <Link href="/" className="inline-flex items-center gap-2 group">
      <span className="h-2 w-2 rounded-full bg-ink transition-transform group-hover:scale-125" />
      <span className="text-lg font-semibold tracking-tight lowercase">juris</span>
    </Link>
  );
}
