import { useEffect, useRef, useState, type FormEvent } from "react";
import { useNavigate, useRouterState } from "@tanstack/react-router";
import { Search } from "lucide-react";
import { useCan } from "@/hooks/useCapabilities";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";

/**
 * The search field of the shell: submitting opens /search with the query.
 * Hidden where it would duplicate the Search page's own field, and for a
 * viewer who cannot search. `shortcut`: "/" focuses it (one instance only).
 */
export function GlobalSearch({
  className,
  wrapperClassName,
  shortcut = false,
}: {
  className?: string;
  /** A container rendered only when the field is (e.g. the M/S row under the top bar). */
  wrapperClassName?: string;
  shortcut?: boolean;
}) {
  const canSearch = useCan("search");
  const onSearchPage = useRouterState({ select: (s) => s.location.pathname === "/search" });
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const { t } = useI18n();

  useEffect(() => {
    if (!shortcut) return;
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing = target && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
      if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        input.current?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [shortcut]);

  if (!canSearch || onSearchPage) return null;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    void navigate({ to: "/search", search: { q, type: "track", providers: [] } });
    setQuery("");
  };

  const form = (
    <form role="search" onSubmit={submit} className={cn("relative", className)}>
      <label className="sr-only" htmlFor={shortcut ? "global-search-xl" : undefined}>
        {t("shell.searchLabel")}
      </label>
      <Search aria-hidden className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
      <input
        ref={input}
        id={shortcut ? "global-search-xl" : undefined}
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={t("shell.searchPlaceholder")}
        aria-label={t("shell.searchLabel")}
        className="h-10 w-full rounded-md border bg-card pl-9 pr-10 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
      {shortcut && (
        <kbd aria-hidden title={t("shell.searchShortcut")} className="absolute right-3 top-1/2 -translate-y-1/2 rounded-xs border px-1.5 text-[11px] text-muted-foreground">
          /
        </kbd>
      )}
    </form>
  );
  return wrapperClassName ? <div className={wrapperClassName}>{form}</div> : form;
}
