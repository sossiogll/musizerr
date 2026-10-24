import type { ReactNode } from "react";
import { Info } from "lucide-react";
import { useUpdates } from "@/hooks/useUpdates";
import { useI18n } from "@/i18n";
import { ATTRIBUTION, COPYRIGHT, LICENSE_URL, NOTICE_URL, SOURCE_URL, UPSTREAM_URL } from "@/lib/project";
import { WEB_VERSION } from "@/lib/version";

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

      <VersionSection />

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

/**
 * The version of the web app for everyone; the server's for admins only (GET /admin/updates), like /health:
 * telling everyone which version runs would tell who looks for a vulnerable one where to find it.
 */
function VersionSection() {
  const { t } = useI18n();
  const { data } = useUpdates();
  // The same build gives both the same version: anything else is a cached page or a server not restarted.
  const differs = !!data && !data.development && WEB_VERSION !== "dev" && WEB_VERSION !== data.current;
  return (
    <Section title={t("about.versionTitle")}>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        <dt className="text-muted-foreground">{t("about.webVersion")}</dt>
        <dd className="font-mono">{WEB_VERSION}</dd>
        {data && (
          <>
            <dt className="text-muted-foreground">{t("about.serverVersion")}</dt>
            <dd className="font-mono">
              {data.current}
              {data.commit && <span className="text-muted-foreground"> ({data.commit.slice(0, 7)})</span>}
            </dd>
          </>
        )}
        {data?.schema && (
          <>
            <dt className="text-muted-foreground">{t("about.databaseSchema")}</dt>
            <dd className="font-mono">
              {data.schema.revision}
              {data.schema.migrated_by && (
                <span className="text-muted-foreground"> ({t("about.schemaMigratedBy")} {data.schema.migrated_by})</span>
              )}
            </dd>
            {data.schema.min_app_version && (
              <>
                <dt className="text-muted-foreground">{t("about.schemaMinVersion")}</dt>
                <dd className="font-mono">{data.schema.min_app_version}</dd>
              </>
            )}
          </>
        )}
        {data?.release_info_unavailable && (
          <>
            <dt className="text-muted-foreground">{t("about.releaseInfo")}</dt>
            <dd>
              {t("about.releaseInfoUnavailable")}
              <span className="text-muted-foreground"> ({t(`about.releaseInfoReasons.${data.release_info_unavailable}`)})</span>
            </dd>
          </>
        )}
      </dl>
      {differs && <p role="status" className="text-sm text-warning">{t("about.versionMismatch")}</p>}
    </Section>
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
