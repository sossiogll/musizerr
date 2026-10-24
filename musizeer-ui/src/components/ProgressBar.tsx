import { cn } from "@/lib/utils";

/**
 * A thin progress bar: determinate with a `percent`, indeterminate without
 * one. With reduced motion the indeterminate bar stays still (a partial
 * fill) instead of sliding.
 */
export function ProgressBar({ percent, label, className }: { percent?: number; label: string; className?: string }) {
  const determinate = percent !== undefined;
  return (
    <span
      role="progressbar"
      aria-label={label}
      aria-valuemin={determinate ? 0 : undefined}
      aria-valuemax={determinate ? 100 : undefined}
      aria-valuenow={determinate ? Math.round(percent) : undefined}
      data-indeterminate={determinate ? undefined : ""}
      className={cn("block h-1.5 overflow-hidden rounded-xs bg-muted", className)}
    >
      <span
        className={cn(
          "block h-full rounded-xs bg-primary",
          determinate
            ? "transition-[width] duration-300 motion-reduce:transition-none"
            : "w-1/3 motion-safe:animate-[slot-indeterminate_1.4s_ease-in-out_infinite] motion-reduce:translate-x-full",
        )}
        style={determinate ? { width: `${percent}%` } : undefined}
      />
    </span>
  );
}
