import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, XCircle } from "lucide-react";
import { patchSettings, type SettingDef } from "@/lib/api-client";
import type { ProvidersResponse } from "@/types";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { useI18n } from "@/i18n";
import { isLockedUnavailable, providersForCategory, settingLabel } from "./settingsHelpers";

interface ProviderRefControlProps {
  def: SettingDef;
  value: unknown;
  providers?: ProvidersResponse;
}

/**
 * Controllo dedicato per `type === "provider_ref"` (Fase 5c, Task 2). Diverso
 * da ogni altro tipo: NON entra mai nella barra di salvataggio in batch di
 * `SettingsArea` — attivare un provider è un'azione esplicita e testata a sé,
 * che il server esegue PRIMA di accettare la scrittura (vedi
 * `SettingsService._enforce_provider_ref_tests`). Questo componente rispecchia
 * quel contratto invece di nasconderlo:
 *
 * - scegliere un nuovo provider dal menu è solo una CANDIDATURA (stato
 *   locale, `draft`) — nessuna richiesta parte finché non si preme "Attiva";
 * - "Attiva" invia SOLO questa chiave via PATCH /settings, mostra lo stato
 *   "in corso" mentre il server esegue il test, e in caso di rifiuto mostra
 *   il messaggio del provider qui accanto lasciando il provider attivo
 *   precedente selezionato (mai la candidatura fallita);
 * - svuotare un riferimento opzionale (tornare a "nessuno") non richiede
 *   alcun test lato server — è quindi immediato, senza passare da "Attiva";
 * - a successo invalida sia i valori delle impostazioni sia GET /providers
 *   (lo stato "usable"/badge di un provider può essere cambiato da questo).
 */
export function ProviderRefControl({ def, value, providers }: ProviderRefControlProps) {
  const { t, tx } = useI18n();
  const queryClient = useQueryClient();
  const options = providersForCategory(providers, def.provider_category);
  const current = value === null || value === undefined ? "" : String(value);
  const [draft, setDraft] = useState(current);
  const [error, setError] = useState<string | null>(null);
  // Rincorre SOLO il valore confermato dal server: distingue "il server ha
  // un nuovo valore effettivo" (da recepire) da "l'utente sta scegliendo
  // una candidatura non ancora confermata" (da non calpestare).
  const lastCurrent = useRef(current);

  useEffect(() => {
    if (current !== lastCurrent.current) {
      lastCurrent.current = current;
      setDraft(current);
    }
  }, [current]);

  const activateMutation = useMutation({
    mutationFn: (next: string) => patchSettings({ [def.key]: next }),
    onMutate: () => setError(null),
    onSuccess: (data) => {
      queryClient.setQueryData(["settings-values"], data);
      queryClient.invalidateQueries({ queryKey: ["providers"] });
      queryClient.invalidateQueries({ queryKey: ["settings-schema"] });
    },
    onError: (err: Error & { response?: { data?: { detail?: string } } }) => {
      setError(err.response?.data?.detail ?? t("settings.activationFailed"));
      // Rifiutato: il provider attivo resta quello di prima, mai la
      // candidatura fallita — il menu deve tornarci sopra.
      setDraft(current);
    },
  });

  const locked = isLockedUnavailable(def, value);
  const busy = activateMutation.isPending;
  const dirty = draft !== current;

  const handleSelectChange = (next: string) => {
    setDraft(next);
    setError(null);
    if (next === "") {
      // Svuotare un riferimento opzionale non è testato dal server: azione
      // immediata, senza bisogno di premere "Attiva".
      activateMutation.mutate(next);
    }
  };

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-2">
        <Select
          aria-label={settingLabel(def, tx)}
          value={draft}
          disabled={locked || busy}
          onChange={(e) => handleSelectChange(e.target.value)}
        >
          {/* Un provider_ref non required è legittimamente opzionale (es.
              sync_provider): serve una scelta esplicita di "nessuno". */}
          {!def.required && <option value="">{t("common.none")}</option>}
          {options.map((p) => (
            <option key={p.name} value={p.name}>
              {p.label}
            </option>
          ))}
        </Select>
        {dirty && draft !== "" && (
          <Button
            size="sm"
            variant="secondary"
            disabled={busy || locked}
            onClick={() => activateMutation.mutate(draft)}
          >
            {busy && <Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" />}
            {busy ? t("settings.activating") : t("settings.activate")}
          </Button>
        )}
      </div>
      {error && (
        <p className="text-xs text-destructive flex items-center gap-1 max-w-xs text-right">
          <XCircle className="w-3.5 h-3.5 shrink-0" />
          {error}
        </p>
      )}
    </div>
  );
}
