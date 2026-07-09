import Link from "next/link";

export function Wordmark() {
  return (
    <Link href="/" className="group inline-flex items-center gap-2.5">
      <span
        className="flex h-7 w-7 -rotate-3 items-center justify-center rounded border-[1.5px]
                   border-ink font-serif text-sm font-bold transition-transform
                   group-hover:rotate-0"
      >
        J
      </span>
      <span className="font-serif text-xl font-semibold tracking-tight">Juris</span>
    </Link>
  );
}
