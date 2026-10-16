import { usePublicProviders } from "@/hooks/usePublicProviders";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface ProviderBadgesProps {
  providers: string[];
  className?: string;
}

/** Display name of a provider id, for every viewer (GET /providers/public). */
export function useProviderLabel() {
  return usePublicProviders().label;
}

export function ProviderBadges({ providers, className }: ProviderBadgesProps) {
  const providerLabel = useProviderLabel();

  if (!providers || providers.length === 0) return null;

  return (
    <div className={cn("flex gap-1 mt-1 flex-wrap", className)}>
      {providers.map((p) => (
        <Badge
          key={p}
          variant="secondary"
          className="text-[10px] px-1.5 py-0 h-4 font-medium"
        >
          {providerLabel(p)}
        </Badge>
      ))}
    </div>
  );
}
