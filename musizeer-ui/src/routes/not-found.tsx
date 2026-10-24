import { Link, useRouterState } from "@tanstack/react-router";
import { Compass } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";

export function NotFoundPage() {
  const path = useRouterState({ select: (s) => s.location.pathname });
  const { t } = useI18n();
  return (
    <section className="mx-auto flex max-w-lg flex-col items-start gap-4 py-16">
      <Compass aria-hidden className="size-8 text-primary" />
      <h1 className="font-display text-2xl font-bold">{t("notFound.title")}</h1>
      <p className="break-all text-muted-foreground">{t("notFound.body", { path })}</p>
      <Button asChild>
        <Link to="/">{t("notFound.goHome")}</Link>
      </Button>
    </section>
  );
}
