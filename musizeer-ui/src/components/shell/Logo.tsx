import { Link } from "@tanstack/react-router";
import { Music } from "lucide-react";
import { cn } from "@/lib/utils";

/** The logo mark, with the name unless `nameClassName` hides it. */
export function Logo({ nameClassName }: { nameClassName?: string }) {
  return (
    <Link to="/" aria-label="Musizeer" className="group flex min-h-11 shrink-0 items-center gap-2.5 rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
      <span className="grid size-9 place-items-center rounded-lg bg-gradient-brand shadow-elevated transition-transform duration-300 group-hover:scale-105">
        <Music aria-hidden className="size-5 text-white" />
      </span>
      <span className={cn("font-display text-xl font-bold tracking-tight text-gradient", nameClassName)}>Musizeer</span>
    </Link>
  );
}
