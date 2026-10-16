import { Link, useRouter, type ErrorComponentProps } from "@tanstack/react-router";
import { useQueryErrorResetBoundary } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { classifyError, type ApiErrorKind } from "@/lib/api-errors";
import { useI18n, type MessageKey } from "@/i18n";
import { isAxiosError } from "axios";

const MESSAGE: Record<ApiErrorKind | "unexpected", MessageKey> = {
  unreachable: "errors.unreachable",
  upstream: "errors.upstream",
  unauthorized: "errors.unauthorized",
  forbidden: "errors.forbidden",
  client: "errors.client",
  unexpected: "errors.unexpected",
};

/**
 * Error boundary of every route: a failure in one page shows here, inside
 * the app shell, instead of blanking the whole app. "Try again" resets the
 * boundary and the failed queries.
 */
export function RouteError({ error, reset }: ErrorComponentProps) {
  const router = useRouter();
  const { reset: resetQueries } = useQueryErrorResetBoundary();
  const { t } = useI18n();
  const kind = isAxiosError(error) ? classifyError(error).kind : "unexpected";

  const retry = () => {
    resetQueries();
    reset();
    void router.invalidate();
  };

  return (
    <section role="alert" className="mx-auto flex max-w-lg flex-col items-start gap-4 py-16">
      <AlertTriangle aria-hidden className="size-8 text-warning" />
      <h1 className="font-display text-2xl font-bold">{t("errors.pageTitle")}</h1>
      <p className="text-muted-foreground">{t(MESSAGE[kind])}</p>
      <div className="flex gap-2">
        <Button onClick={retry}>{t("errors.retry")}</Button>
        <Button variant="outline" asChild>
          <Link to="/">{t("errors.goHome")}</Link>
        </Button>
      </div>
    </section>
  );
}
