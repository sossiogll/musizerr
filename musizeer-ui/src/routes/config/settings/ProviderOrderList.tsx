import { ChevronUp, ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useI18n } from "@/i18n";
import type { ProviderInfo } from "@/types";

interface ProviderOrderListProps {
  order: string[];
  providers: ProviderInfo[];
  onChange: (order: string[]) => void;
  /** Fase 5c, Task 1: true quando la voce non è disponibile ED è già al suo
   * valore di default (`isLockedUnavailable`) — nulla da riordinare che
   * sopravviva al salvataggio, quindi le frecce di spostamento sono bloccate. */
  disabled?: boolean;
}

/**
 * Riordino via frecce su/giù per un'impostazione `type === "list"`. Stessa
 * interazione visiva di `ProviderPriority` in GeneralTab.tsx (Fase < 5a),
 * riusata qui invece di riscritta: unica differenza, non ha più un proprio
 * pulsante "Salva" — l'ordine corrente è un valore controllato come ogni
 * altro campo dell'area, che entra nello stesso stato "modifiche non
 * salvate" e nello stesso salvataggio in batch di SettingsArea.
 */
export function ProviderOrderList({ order, providers, onChange, disabled }: ProviderOrderListProps) {
  const { t } = useI18n();
  const labelOf = (name: string) => providers.find((p) => p.name === name)?.label ?? name;

  const move = (idx: number, dir: -1 | 1) => {
    const next = [...order];
    const swap = idx + dir;
    if (swap < 0 || swap >= next.length) return;
    [next[idx], next[swap]] = [next[swap], next[idx]];
    onChange(next);
  };

  return (
    <div className="space-y-1">
      {order.map((name, idx) => (
        <Card key={name} className="flex items-center gap-2 px-3 py-1">
          <span className="text-xs text-muted-foreground w-4 text-right">{idx + 1}.</span>
          <span className="flex-1 text-sm">{labelOf(name)}</span>
          <div className="flex gap-0.5">
            <Button
              variant="ghost"
              size="icon"
              className="size-11 sm:size-9"
              aria-label={t("settings.moveUp", { name: labelOf(name) })}
              onClick={() => move(idx, -1)}
              disabled={disabled || idx === 0}
            >
              <ChevronUp aria-hidden className="size-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="size-11 sm:size-9"
              aria-label={t("settings.moveDown", { name: labelOf(name) })}
              onClick={() => move(idx, 1)}
              disabled={disabled || idx === order.length - 1}
            >
              <ChevronDown aria-hidden className="size-4" />
            </Button>
          </div>
        </Card>
      ))}
    </div>
  );
}
