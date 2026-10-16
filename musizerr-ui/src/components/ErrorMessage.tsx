import { classifyError } from "@/lib/api-errors";
import { useErrorText } from "@/lib/error-text";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";

interface ErrorMessageProps {
  code: string | null | undefined;
  message: string | null | undefined;
  /** Raw error text: the backend only sends it to admins. */
  detail?: string | null;
  className?: string;
}

/**
 * A backend error in the viewer's language, with the raw detail in a
 * collapsed block when the server included it (admins only — decided by the
 * server, never here).
 */
export function ErrorMessage({ code, message, detail, className }: ErrorMessageProps) {
  const errorText = useErrorText();
  const { t } = useI18n();
  return (
    <div className={cn("space-y-1", className)}>
      <p>{errorText(code, message)}</p>
      {detail && (
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer select-none">{t("errors.showDetails")}</summary>
          <pre className="mt-1 whitespace-pre-wrap break-all rounded-sm bg-muted p-2 font-mono">{detail}</pre>
        </details>
      )}
    </div>
  );
}

/** A failed API call in words: the backend's error code when it sent one, `fallback` otherwise. */
export function ApiErrorMessage({ error, fallback, className }: { error: unknown; fallback: string; className?: string }) {
  const { body } = classifyError(error);
  return <ErrorMessage className={className} code={body?.error_code} message={body?.error_message ?? fallback} detail={body?.error_detail} />;
}
