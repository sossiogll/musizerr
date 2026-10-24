import type { ReactNode } from "react";
import { useRouterState } from "@tanstack/react-router";
import { OfflineBanner } from "@/components/connectivity/OfflineBanner";
import { Player } from "@/components/Player";
import { usePlayer } from "@/contexts/PlayerContext";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import { AccountMenu } from "./AccountMenu";
import { AppBackground } from "./AppBackground";
import { GlobalSearch } from "./GlobalSearch";
import { LibraryJobIndicator } from "./LibraryJobIndicator";
import { LibraryJobsWatcher } from "./LibraryJobsWatcher";
import { RequestsWatcher } from "./RequestsWatcher";
import { Logo } from "./Logo";
import { NavList } from "./NavList";
import { PRIMARY_NAV, SECONDARY_NAV, isActive } from "./nav-items";
import { ServiceStatus } from "./ServiceStatus";

/**
 * The one app shell. The navigation (nav-items.ts) is rendered, by CSS
 * breakpoints only, as:
 *   XL ≥1280  a 240px sidebar with the user and the service status at the bottom;
 *   L  1024+  a top bar, icon + label, search inline;
 *   M  768+   a top bar, labels only, logo mark, search in a row below;
 *   S  600+   a top bar of 44px icons, search in a row below;
 *   XS <600   a bottom bar with the 4 primary items, account from the avatar.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const { track } = usePlayer();
  const { t } = useI18n();
  const hasPlayer = !!track;

  return (
    <div
      className={cn(
        // `isolate`: the shell paints its own background first, then the
        // animated one (-z-10) above it and under everything else.
        "isolate min-h-dvh bg-background text-foreground xl:pl-60",
        // The fixed chrome's size, for content pinned between it (the track lyrics):
        // the top bar (none on XL) and the player (with its top border).
        "[--shell-top:3.5rem] sm:[--shell-top:4rem] xl:[--shell-top:0px]",
        hasPlayer ? "[--player-h:calc(4rem_+_1px)] sm:[--player-h:calc(5rem_+_1px)]" : "[--player-h:0px]",
      )}
    >
      <AppBackground />
      <a
        href="#content"
        className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-[60] focus:rounded-md focus:bg-primary focus:px-4 focus:py-2 focus:text-sm focus:font-medium focus:text-primary-foreground"
      >
        {t("shell.skipToContent")}
      </a>
      <aside
        aria-label="Musizerr"
        className="fixed inset-y-0 left-0 z-40 hidden w-60 flex-col gap-6 border-r bg-card/60 px-3 py-5 xl:flex"
      >
        <div className="px-2">
          <Logo />
        </div>
        <nav aria-label={t("nav.main")} className="flex flex-1 flex-col gap-4">
          <NavList items={PRIMARY_NAV} variant="sidebar" className="space-y-1" />
          <div className="border-t" />
          <NavList items={SECONDARY_NAV} variant="sidebar" className="space-y-1" />
        </nav>
        <LibraryJobIndicator withText />
        <AccountMenu block />
      </aside>

      <header className="sticky top-0 z-40 hidden h-16 items-center gap-2 border-b px-4 glass sm:flex xl:hidden">
        <Logo nameClassName="hidden lg:inline" />
        <nav aria-label={t("nav.main")} className="flex items-center gap-0.5">
          <NavList items={PRIMARY_NAV} variant="top" className="flex items-center gap-0.5" />
          <NavList items={SECONDARY_NAV} variant="top" className="flex items-center gap-0.5" />
        </nav>
        <div className="ml-auto flex min-w-0 flex-1 items-center justify-end gap-2">
          <GlobalSearch className="hidden w-full max-w-60 lg:block" />
          <LibraryJobIndicator />
          <ServiceStatus />
          <AccountMenu />
        </div>
      </header>

      <MobileHeader />

      <GlobalSearch wrapperClassName="hidden border-b px-4 py-2 sm:block lg:hidden" />

      <OfflineBanner />

      <main
        id="content"
        tabIndex={-1}
        className={cn(
          "mx-auto w-full max-w-[1600px] px-4 pt-4 focus:outline-none md:px-6 lg:px-8 lg:pt-6 xl:px-10",
          // Room for the fixed bars below the content (bottom bar on XS, player).
          hasPlayer ? "pb-44 sm:pb-28" : "pb-24 sm:pb-8",
        )}
      >
        <GlobalSearch shortcut className="mb-6 hidden max-w-xl xl:block" />
        {children}
      </main>

      <Player />
      <LibraryJobsWatcher />
      <RequestsWatcher />

      <nav
        aria-label={t("nav.main")}
        className="fixed inset-x-0 bottom-0 z-50 border-t bg-background/95 pb-[env(safe-area-inset-bottom)] backdrop-blur sm:hidden"
      >
        <NavList items={PRIMARY_NAV} variant="bottom" className="flex h-[68px]" />
      </nav>
    </div>
  );
}

/** XS: the page name and the account menu (Settings and Admin live there). */
function MobileHeader() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const { t } = useI18n();
  const current = [...PRIMARY_NAV, ...SECONDARY_NAV].find((item) => isActive(item, pathname));
  return (
    <header className="sticky top-0 z-40 flex h-14 items-center justify-between gap-2 px-4 glass sm:hidden">
      {current ? (
        <span className="font-display text-xl font-bold">{t(current.labelKey)}</span>
      ) : (
        <Logo />
      )}
      <span className="flex items-center">
        <LibraryJobIndicator />
        <AccountMenu />
      </span>
    </header>
  );
}
