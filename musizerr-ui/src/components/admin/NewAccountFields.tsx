import { useId } from "react";
import { Input } from "@/components/ui/input";
import { useI18n, type MessageKey } from "@/i18n";
import { MIN_PASSWORD_LENGTH } from "@/lib/password";

export interface NewAccount {
  username: string;
  email: string;
  password: string;
  confirm: string;
}

export const EMPTY_ACCOUNT: NewAccount = { username: "", email: "", password: "", confirm: "" };

/** Why the form cannot be sent yet (a dictionary key), or null. The server judges again. */
export function accountProblem(account: NewAccount): MessageKey | null {
  if (account.username.trim() === "") return "admin.new.usernameRequired";
  if (account.password.length < MIN_PASSWORD_LENGTH) return "admin.new.passwordShort";
  if (account.password !== account.confirm) return "auth.passwordsMismatch";
  return null;
}

/**
 * The fields of a new account: username, an optional email, the password twice. The password rules are
 * said under the field; the dialog that owns the form decides when it is complete (`accountProblem`).
 */
export function NewAccountFields({
  value,
  onChange,
  withEmail = true,
}: {
  value: NewAccount;
  onChange: (next: NewAccount) => void;
  withEmail?: boolean;
}) {
  const { t } = useI18n();
  const ids = { username: useId(), email: useId(), password: useId(), confirm: useId() };
  const set = (field: keyof NewAccount) => (e: React.ChangeEvent<HTMLInputElement>) => onChange({ ...value, [field]: e.target.value });
  const showMismatch = value.confirm !== "" && value.password !== value.confirm;

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <label htmlFor={ids.username} className="text-sm font-medium">
          {t("auth.username")}
        </label>
        <Input id={ids.username} autoComplete="off" required value={value.username} onChange={set("username")} className="h-11 sm:h-9" />
      </div>
      {withEmail && (
        <div className="space-y-1.5">
          <label htmlFor={ids.email} className="text-sm font-medium">
            {t("auth.email")} <span className="text-muted-foreground">{t("auth.emailOptional")}</span>
          </label>
          <Input id={ids.email} type="email" autoComplete="off" value={value.email} onChange={set("email")} className="h-11 sm:h-9" />
        </div>
      )}
      <div className="space-y-1.5">
        <label htmlFor={ids.password} className="text-sm font-medium">
          {t("auth.password")}
        </label>
        <Input
          id={ids.password}
          type="password"
          autoComplete="new-password"
          required
          value={value.password}
          onChange={set("password")}
          className="h-11 sm:h-9"
        />
        <p className="text-xs text-muted-foreground">{t("admin.new.passwordHint", { count: MIN_PASSWORD_LENGTH })}</p>
      </div>
      <div className="space-y-1.5">
        <label htmlFor={ids.confirm} className="text-sm font-medium">
          {t("auth.confirmPassword")}
        </label>
        <Input
          id={ids.confirm}
          type="password"
          autoComplete="new-password"
          required
          value={value.confirm}
          onChange={set("confirm")}
          className="h-11 sm:h-9"
        />
        {showMismatch && (
          <p role="alert" className="text-xs text-destructive">
            {t("auth.passwordsMismatch")}
          </p>
        )}
      </div>
    </div>
  );
}
