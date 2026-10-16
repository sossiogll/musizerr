import type { ReactNode } from "react";
import { Info } from "lucide-react";
import { useI18n } from "@/i18n";
import { ATTRIBUTION, COPYRIGHT, LICENSE_URL, NOTICE_URL, SOURCE_URL, UPSTREAM_URL } from "@/lib/project";

/**
 * The Appropriate Legal Notices (AGPL section 5(d)) and the attribution of NOTICE.md:
 * open to every viewer, guests included, and linked from the main navigation.
 */
export function AboutPage() {
  const { t } = useI18n();
  return (
    <article className="mx-auto flex max-w-2xl flex-col gap-8 py-8">
      <header className="flex items-center gap-3">
        <Info aria-hidden className="size-8 text-primary" />
        <h1 className="font-display text-3xl font-bold">{t("about.title")}</h1>
      </header>
      <p className="text-muted-foreground">{t("about.intro")}</p>

      <blockquote className="rounded-lg border-l-4 border-primary bg-card px-5 py-4 text-base">
        {ATTRIBUTION.replace(UPSTREAM_URL, "")}
        <ExternalLink href={UPSTREAM_URL}>{UPSTREAM_URL}</ExternalLink>
      </blockquote>

      <Section title={t("about.sourceTitle")}>
        <p>
          {t("about.source")} <ExternalLink href={SOURCE_URL}>{SOURCE_URL}</ExternalLink>
        </p>
      </Section>

      <Section title={t("about.licenseTitle")}>
        <p>{COPYRIGHT}</p>
        <p>{t("about.license")}</p>
        <p>{t("about.sdkLicense")}</p>
        <p>{t("about.providers")}</p>
        <p>{t("about.warranty")}</p>
        <p className="flex flex-wrap gap-x-4 gap-y-1">
          <ExternalLink href={LICENSE_URL}>{t("about.licenseText")}</ExternalLink>
          <ExternalLink href={NOTICE_URL}>{t("about.noticeText")}</ExternalLink>
        </p>
      </Section>
    </article>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h2 className="font-display text-xl font-semibold">{title}</h2>
      {children}
    </section>
  );
}

function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noreferrer" className="font-medium text-primary underline-offset-4 hover:underline">
      {children}
    </a>
  );
}
