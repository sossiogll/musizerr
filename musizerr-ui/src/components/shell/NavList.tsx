import { Link, useRouterState } from "@tanstack/react-router";
import { useCapabilities } from "@/hooks/useCapabilities";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import { CountBadge } from "./CountBadge";
import { allowed, isActive, type NavItem } from "./nav-items";
import { useNavCounts } from "./useNavCounts";

export type NavVariant = "sidebar" | "top" | "bottom";

const LINK: Record<NavVariant, string> = {
  sidebar: "flex h-11 items-center gap-3 rounded-md px-3 text-sm font-medium",
  // L: icon + label; M: label only; S: 44px icon only (label stays for screen readers).
  top: "relative inline-flex h-11 items-center gap-2 rounded-md px-2.5 text-sm font-medium whitespace-nowrap max-md:w-11 max-md:justify-center max-md:px-0",
  bottom: "relative flex h-full flex-col items-center justify-center gap-1 text-[11px] font-medium",
};

const ICON: Record<NavVariant, string> = {
  sidebar: "size-5 shrink-0",
  top: "size-5 shrink-0 md:max-lg:hidden",
  bottom: "size-[22px]",
};

const LABEL: Record<NavVariant, string> = {
  sidebar: "truncate",
  top: "max-md:sr-only",
  bottom: "",
};

/** The allowed items of `items`, rendered for one form of the shell. */
export function NavList({ items, variant, className }: { items: readonly NavItem[]; variant: NavVariant; className?: string }) {
  const { can } = useCapabilities();
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const navCount = useNavCounts();
  const { t } = useI18n();

  return (
    <ul className={className}>
      {items
        .filter((item) => allowed(item, can))
        .filter((item) => !(variant === "top" && item.searchField && can.search))
        .map((item) => {
          const active = isActive(item, pathname);
          const { count, label } = navCount(item);
          const Icon = item.icon;
          return (
            <li key={item.to} className={variant === "bottom" ? "h-full min-w-0 flex-1" : undefined}>
              <Link
                to={item.to}
                aria-current={active ? "page" : undefined}
                className={cn(
                  LINK[variant],
                  "transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  active
                    ? variant === "bottom"
                      ? "text-primary"
                      : "bg-primary/15 text-foreground"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                  variant === "bottom" && "hover:bg-transparent",
                )}
              >
                <Icon aria-hidden className={ICON[variant]} />
                <span className={LABEL[variant]}>
                  {t(variant === "top" ? (item.shortLabelKey ?? item.labelKey) : item.labelKey)}
                </span>
                {count > 0 && <NavBadge count={count} label={label} variant={variant} />}
              </Link>
            </li>
          );
        })}
    </ul>
  );
}

function NavBadge({ count, label, variant }: { count: number; label: string; variant: NavVariant }) {
  return (
    <CountBadge
      count={count}
      label={label}
      className={cn(
        variant === "sidebar" && "ml-auto",
        variant === "top" && "max-md:absolute max-md:right-0 max-md:top-0.5",
        variant === "bottom" && "absolute left-[54%] top-2 h-4 min-w-4 px-1",
      )}
    />
  );
}
