import { cn } from "@/lib/utils";

/**
 * A small counter (a rounded rectangle, never a pill) for something that
 * needs a look: downloads in progress, requests waiting for an admin. Past 99
 * it says "99+". `label` is what a screen reader hears; `className` places it.
 */
export function CountBadge({ count, label, className }: { count: number; label: string; className?: string }) {
  return (
    <span
      aria-label={label}
      className={cn("grid h-5 min-w-5 place-items-center rounded-sm bg-primary px-1.5 text-[10px] font-bold text-primary-foreground", className)}
    >
      {count > 99 ? "99+" : count}
    </span>
  );
}
