import { Link, useNavigate } from "@tanstack/react-router";
import { LogIn, LogOut } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAuth } from "@/contexts/AuthContext";
import { useCapabilities } from "@/hooks/useCapabilities";
import { useSignInSearch } from "@/hooks/useSignInSearch";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import { CountBadge } from "./CountBadge";
import { SECONDARY_NAV, allowed } from "./nav-items";
import { useNavCounts } from "./useNavCounts";
import { ServiceStatus } from "./ServiceStatus";

/**
 * Who is signed in, and the way to Settings, Administration and sign-out.
 * On XS it is the only way to the secondary items. A guest gets "Sign in".
 * `block`: the XL sidebar form (name and service status beside the avatar).
 */
export function AccountMenu({ block = false }: { block?: boolean }) {
  const { user, logout } = useAuth();
  const { mode, can } = useCapabilities();
  const navigate = useNavigate();
  const { t } = useI18n();
  const signIn = useSignInSearch();
  const navCount = useNavCounts();

  if (mode === "guest") {
    const signInButton = (
      <Button asChild variant={block ? "outline" : "ghost"} size="sm" className={cn("h-10", block && "w-full")}>
        <Link to="/login" search={signIn}>
          <LogIn aria-hidden />
          {t("nav.signIn")}
        </Link>
      </Button>
    );
    if (block) return signInButton;
    // On XS this menu is the only way to the secondary items, and a guest has no menu: the
    // items a guest may open (About) sit beside "Sign in". From S up the top bar shows them.
    const guestItems = SECONDARY_NAV.filter((item) => allowed(item, can));
    return (
      <span className="flex items-center">
        {guestItems.map((item) => (
          <Button key={item.to} asChild variant="ghost" size="icon" className="sm:hidden">
            <Link to={item.to} aria-label={t(item.labelKey)}>
              <item.icon aria-hidden />
            </Link>
          </Button>
        ))}
        {signInButton}
      </span>
    );
  }

  const name = user?.username ?? t("shell.openMode");
  const role = mode === "open" ? t("shell.openMode") : user?.role === "admin" ? t("shell.roleAdmin") : t("shell.roleUser");
  const items = SECONDARY_NAV.filter((item) => allowed(item, can));
  // What waits in the items behind this menu (Administration's requests), where the
  // bar has no room for them: on the avatar of the XS header (from S up the top bar and
  // the sidebar show the item itself).
  const waiting = items.map(navCount).find((entry) => entry.count > 0);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={t("nav.accountMenu")}
          className={cn(
            "relative flex items-center gap-3 rounded-lg text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            block ? "w-full p-2 hover:bg-muted" : "size-11 justify-center rounded-md hover:bg-muted",
          )}
        >
          <Avatar name={name} />
          {!block && waiting && <CountBadge count={waiting.count} label={waiting.label} className="absolute right-0 top-0 h-4 min-w-4 px-1 sm:hidden" />}
          {block && (
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium">{name}</span>
              <ServiceStatus withText />
            </span>
          )}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuLabel className="space-y-0.5">
          <span className="block truncate">{name}</span>
          <span className="block text-xs font-normal text-muted-foreground">{role}</span>
        </DropdownMenuLabel>
        {items.length > 0 && <DropdownMenuSeparator />}
        {items.map((item) => {
          const { count, label } = navCount(item);
          return (
            <DropdownMenuItem key={item.to} onSelect={() => void navigate({ to: item.to })}>
              <item.icon aria-hidden className="mr-2 size-4" />
              {t(item.labelKey)}
              {count > 0 && <CountBadge count={count} label={label} className="ml-auto" />}
            </DropdownMenuItem>
          );
        })}
        {user && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onSelect={async () => {
                await logout();
                void navigate({ to: "/" });
              }}
            >
              <LogOut aria-hidden className="mr-2 size-4" />
              {t("nav.signOut")}
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function Avatar({ name }: { name: string }) {
  return (
    <span aria-hidden className="grid size-8 shrink-0 place-items-center rounded-full bg-secondary text-sm font-semibold uppercase">
      {name.slice(0, 1)}
    </span>
  );
}
