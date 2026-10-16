import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useI18n } from "@/i18n";

/**
 * The page an OAuth2 authorization comes back to (`?auth=success`, set by
 * GET /auth/{provider}/callback): says it worked, refreshes the accounts and
 * clears the mark from the address.
 */
export function useAuthReturn(): void {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("auth") !== "success") return;
    toast.success(t("linkedAccounts.authSuccess"));
    void queryClient.invalidateQueries({ queryKey: ["linked-accounts"] });
    void queryClient.invalidateQueries({ queryKey: ["providers"] });
    params.delete("auth");
    const query = params.toString();
    window.history.replaceState(window.history.state, "", `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`);
  }, [queryClient, t]);
}
