import { useState } from "react";
import { Loader2, XCircle } from "lucide-react";
import { useI18n } from "@/i18n";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { CredentialField } from "@/types";

/**
 * The login form of a `credentials` account, built from the fields the
 * provider declares (`ProviderInfo.credential_fields`): never a fixed
 * username and password. A `secret` field is masked and never shown back.
 */
export function CredentialFieldsForm({
  fields,
  saving,
  error,
  onSubmit,
  onCancel,
  onEdit,
  hint,
  submitLabel,
}: {
  fields: CredentialField[];
  saving?: boolean;
  error?: string | null;
  onSubmit: (values: Record<string, string>) => void;
  onCancel?: () => void;
  onEdit?: () => void;
  /** What the form asks for; the linking wording unless given. */
  hint?: string;
  /** The submit button; "Save" unless given. */
  submitLabel?: string;
}) {
  const { t } = useI18n();
  const [values, setValues] = useState<Record<string, string>>({});
  const complete = fields.every((f) => values[f.key]);

  return (
    <div className="space-y-2">
      <p className="text-xs text-muted-foreground">{hint ?? t("linkedAccounts.enterCredentials")}</p>
      <div className="flex flex-wrap gap-2">
        {fields.map((f) => (
          <Input
            key={f.key}
            type={f.secret ? "password" : "text"}
            autoComplete="off"
            aria-label={f.label}
            placeholder={f.label}
            value={values[f.key] ?? ""}
            onChange={(e) => {
              setValues((v) => ({ ...v, [f.key]: e.target.value }));
              onEdit?.();
            }}
            className="min-w-40 flex-1"
          />
        ))}
      </div>
      <div className="flex gap-2">
        <Button variant="secondary" onClick={() => onSubmit(values)} disabled={!complete || saving}>
          {saving && <Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" />}
          {submitLabel ?? t("common.save")}
        </Button>
        {onCancel && (
          <Button variant="ghost" onClick={onCancel}>
            {t("providersTab.cancel")}
          </Button>
        )}
      </div>
      {error && (
        <p className="text-xs text-destructive flex items-center gap-1">
          <XCircle className="w-3.5 h-3.5 shrink-0" />
          {error}
        </p>
      )}
    </div>
  );
}
