import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { getPublicProviders } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";

export interface ProviderRef {
  id: string;
  label: string;
}

/** The capability key (sdk/src/musizeer_sdk/capabilities.py) of a provider that gives an artist's whole discography to download. */
export const DISCOGRAPHY_CAPABILITY = "discography";

export interface PublicProvidersView {
  /** Display name of a provider id (the id itself when unknown). */
  label: (id: string) => string;
  /** Whether the metadata provider declares the capability (false until the declarations are loaded). */
  can: (id: string, capability: string) => boolean;
  /** Metadata providers in the viewer's priority order. */
  metadata: ProviderRef[];
  /** The viewer's metadata priority (ids). */
  priority: string[];
}

/**
 * Provider labels, the metadata priority and the capabilities each metadata
 * provider declares, from GET /providers/public — readable by every viewer,
 * guest included, so no label is ever a raw id and no page needs the
 * admin/user-only GET /providers just to name a provider.
 */
export function usePublicProviders(): PublicProvidersView {
  const { data } = useQuery({
    queryKey: queryKeys.providersPublic,
    queryFn: getPublicProviders,
    staleTime: 10 * 60 * 1000,
  });
  return useMemo(() => {
    const labels = new Map((data?.providers ?? []).map((p) => [p.id, p.label]));
    const label = (id: string) => labels.get(id) ?? id;
    const can = (id: string, capability: string) => data?.metadata_capabilities[id]?.includes(capability) ?? false;
    return {
      label,
      can,
      metadata: (data?.metadata ?? []).map((id) => ({ id, label: label(id) })),
      priority: data?.metadata_priority ?? [],
    };
  }, [data]);
}
