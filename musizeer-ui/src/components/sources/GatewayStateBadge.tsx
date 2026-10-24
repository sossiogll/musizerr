import { Badge, type BadgeProps } from "@/components/ui/badge";
import { useI18n } from "@/i18n";
import type { GatewayProviderStateName } from "@/types";

const VARIANT: Record<GatewayProviderStateName, NonNullable<BadgeProps["variant"]>> = {
  running: "success",
  pulling: "info",
  starting: "info",
  restarting: "warning",
  refused: "destructive",
  unavailable: "destructive",
  failed: "destructive",
  stopped: "secondary",
};

/** What the gateway says a provider is doing. `undefined`: it has not heard of it yet. */
export function GatewayStateBadge({ state }: { state: GatewayProviderStateName | undefined }) {
  const { t } = useI18n();
  if (!state) return <Badge variant="outline">{t("sources.state.unknown")}</Badge>;
  return <Badge variant={VARIANT[state]}>{t(`sources.state.${state}`)}</Badge>;
}
