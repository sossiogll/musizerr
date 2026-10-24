import type { ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { CoverImage } from "@/components/CoverImage";
import { useProviderLabel } from "@/components/ProviderBadges";
import { useI18n } from "@/i18n";
import type { ExternalId, ItemKind } from "@/types";

interface DetailHeaderProps {
  kind: ItemKind;
  cover: string | null | undefined;
  /** Small line above the title (e.g. "Album · 2021"). */
  eyebrow?: ReactNode;
  title: string;
  /** Under the title: artist links, a description. */
  subtitle?: ReactNode;
  /** Facts in one line: track count, duration. */
  meta?: ReactNode;
  /** Below the facts: the library coverage. */
  children?: ReactNode;
  actions?: ReactNode;
}

/**
 * The top of a track, album, artist or playlist page: a large cover with the
 * title beside it (stacked and centred on a phone).
 */
export function DetailHeader({ kind, cover, eyebrow, title, subtitle, meta, children, actions }: DetailHeaderProps) {
  return (
    <header className="flex flex-col items-center gap-5 sm:flex-row sm:items-end">
      <CoverImage
        src={cover}
        alt=""
        type={kind}
        size="lg"
        className={kind === "artist" ? "size-40 sm:size-44 lg:size-52" : "size-44 rounded-xl shadow-elevated sm:size-48 lg:size-56"}
      />
      <div className="w-full min-w-0 flex-1 space-y-2 text-center sm:text-left">
        {eyebrow && <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">{eyebrow}</p>}
        <h1 className="break-words font-display text-3xl font-bold leading-tight lg:text-4xl xl:text-5xl">{title}</h1>
        {subtitle && <div className="text-base text-muted-foreground">{subtitle}</div>}
        {meta && <p className="text-sm text-muted-foreground">{meta}</p>}
        {children && <div className="mx-auto max-w-md sm:mx-0">{children}</div>}
        {actions && <div className="flex flex-wrap items-center justify-center gap-2 pt-2 sm:justify-start">{actions}</div>}
      </div>
    </header>
  );
}

/**
 * Identifiers and source providers, collapsed: useful to check a match, not
 * something to read every time.
 */
export function TechnicalDetails({ providers, externalIds, children }: { providers?: string[]; externalIds?: ExternalId[]; children?: ReactNode }) {
  const { t } = useI18n();
  const label = useProviderLabel();
  if (!providers?.length && !externalIds?.length && !children) return null;
  return (
    <details className="group rounded-lg border px-4 py-3 text-sm">
      <summary className="flex min-h-11 cursor-pointer select-none list-none items-center justify-between gap-2 font-medium [&::-webkit-details-marker]:hidden">
        {t("details.technical")}
        <ChevronDown aria-hidden className="size-4 text-muted-foreground transition-transform group-open:rotate-180" />
      </summary>
      <dl className="mt-2 grid gap-x-6 gap-y-2 sm:grid-cols-[max-content_1fr]">
        {!!providers?.length && (
          <>
            <dt className="text-muted-foreground">{t("details.sources")}</dt>
            <dd>{providers.map(label).join(", ")}</dd>
          </>
        )}
        {externalIds?.map((eid) => (
          <div key={`${eid.type}:${eid.provider ?? ""}:${eid.value}`} className="contents">
            <dt className="text-muted-foreground">
              {eid.type}
              {eid.provider ? ` · ${label(eid.provider)}` : ""}
            </dt>
            <dd className="break-all font-mono text-xs leading-5">{eid.value}</dd>
          </div>
        ))}
        {children}
      </dl>
    </details>
  );
}
