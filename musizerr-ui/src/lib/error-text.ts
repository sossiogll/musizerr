import { useCallback } from "react";
import { toast } from "sonner";
import { en } from "@/i18n/messages";
import { useI18n } from "@/i18n";
import { classifyError } from "@/lib/api-errors";

// The kinds the dictionary can translate, longest first so that a kind that
// ends with another one ("upstream_error" vs "error") wins.
const KINDS = Object.keys(en.error_kinds).sort((a, b) => b.length - a.length);

/**
 * The readable text of a backend error code (`<subject>_<kind>`, see
 * services/errors.py): a whole-code translation if there is one
 * (error_codes.*), else subject + kind, else the backend's own message.
 */
export function useErrorText() {
  const { tx } = useI18n();
  return useCallback(
    (code: string | null | undefined, fallback: string | null | undefined): string => {
      const message = fallback ?? "";
      if (!code) return message;
      const whole = tx(`error_codes.${code}`, "");
      if (whole) return whole;
      const kind = KINDS.find((k) => code.endsWith(`_${k}`));
      if (!kind) return message;
      const subject = tx(`error_subjects.${code.slice(0, -(kind.length + 1))}`, "");
      if (!subject) return message;
      return tx(`error_kinds.${kind}`, message, { subject });
    },
    [tx],
  );
}

/**
 * Says in a toast that an API call failed: the backend's error code in the
 * viewer's language when it sent one (`useErrorText`), `fallback` otherwise.
 */
export function useFailureToast() {
  const errorText = useErrorText();
  return useCallback(
    (error: unknown, fallback: string) => {
      const { body } = classifyError(error);
      toast.error(errorText(body?.error_code, body?.error_message ?? fallback));
    },
    [errorText],
  );
}
