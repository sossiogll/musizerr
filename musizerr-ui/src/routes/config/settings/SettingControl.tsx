import { useState } from "react";
import type { SettingDef } from "@/lib/api-client";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useI18n } from "@/i18n";
import { settingLabel } from "./settingsHelpers";

interface SettingControlProps {
  def: SettingDef;
  value: unknown;
  onChange: (value: unknown) => void;
  /** Fase 5c, Task 1: true quando la voce non è disponibile ED è già al suo
   * valore di default — nulla da spegnere, quindi il controllo va bloccato
   * del tutto (vedi `isLockedUnavailable`, mai calcolato qui). */
  disabled?: boolean;
}


/**
 * Un unico controllo scelto dal `type` dichiarato dal backend — mai una
 * mappa hardcoded lato UI. `type === "list"` NON passa di qui: ha bisogno
 * dell'elenco provider e della logica di merge/riordino, gestita da
 * `ProviderOrderList` (vedi SettingsArea). `type === "provider_ref"` non
 * passa più di qui da Fase 5c, Task 2: attivarlo è un'azione esplicita e
 * testata a parte (`ProviderRefControl`), mai un campo del salvataggio
 * in batch.
 */
export function SettingControl({
  def,
  value,
  onChange,
  disabled,
}: SettingControlProps) {
  const { t, tx } = useI18n();
  // A value the backend declares risky (`def.confirm`) is applied only after
  // an explicit confirmation; any other value goes through at once.
  const [pending, setPending] = useState<{ value: unknown } | null>(null);
  const guardedChange = (next: unknown) => {
    if (def.confirm && next === def.confirm.value && value !== next)
      setPending({ value: next });
    else onChange(next);
  };

  return (
    <>
      <ControlInput
        def={def}
        value={value}
        onChange={guardedChange}
        disabled={disabled}
      />
      {def.confirm && (
        <ConfirmDialog
          open={pending !== null}
          onOpenChange={(open) => !open && setPending(null)}
          title={t("settings.confirmTitle")}
          description={tx(def.confirm.message_key, def.confirm.message)}
          confirmLabel={t("common.confirm")}
          destructive
          onConfirm={() => {
            if (pending) onChange(pending.value);
            setPending(null);
          }}
        />
      )}
    </>
  );
}

function ControlInput({ def, value, onChange, disabled }: SettingControlProps) {
  const { t, tx } = useI18n();

  switch (def.type) {
    case "bool":
      return (
        <Switch
          checked={!!value}
          onCheckedChange={onChange}
          aria-label={settingLabel(def, tx)}
          disabled={disabled}
        />
      );

    case "enum": {
      const current =
        value === null || value === undefined ? "" : String(value);
      return (
        <Select
          aria-label={settingLabel(def, tx)}
          value={current}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
        >
          {current === "" && (
            <option value="" disabled>
              {t("common.none")}
            </option>
          )}
          {(def.options ?? []).map((o, i) => (
            <option key={o} value={o}>
              {tx(def.option_label_keys?.[i] ?? "", o)}
            </option>
          ))}
        </Select>
      );
    }

    case "int":
    case "float":
      return (
        <Input
          type="number"
          className="w-28"
          aria-label={settingLabel(def, tx)}
          value={value === null || value === undefined ? "" : String(value)}
          min={def.minimum ?? undefined}
          max={def.maximum ?? undefined}
          step={def.step ?? (def.type === "float" ? 0.01 : 1)}
          onChange={(e) =>
            onChange(e.target.value === "" ? null : Number(e.target.value))
          }
          disabled={disabled}
        />
      );

    case "string":
      return (
        <Input
          type={def.secret ? "password" : "text"}
          className="w-56"
          aria-label={settingLabel(def, tx)}
          placeholder={def.secret ? "••••••" : ""}
          value={value === null || value === undefined ? "" : String(value)}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
        />
      );

    default:
      return null;
  }
}
