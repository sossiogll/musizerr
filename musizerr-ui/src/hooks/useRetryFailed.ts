import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { retryFailedDownloads } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { useQueueActions } from "@/contexts/QueueContext";
import { useI18n } from "@/i18n";

/**
 * "Retry all failed" (POST /downloads/retry-failed): the server picks what to
 * retry; the new downloads join the live progress and the lists refresh.
 */
export function useRetryFailed() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const addItems = useQueueActions();
  return useMutation({
    mutationFn: () => retryFailedDownloads(),
    onSuccess: ({ download_ids }) => {
      addItems(download_ids);
      toast.success(download_ids.length ? t("downloads.retriedAll", { count: download_ids.length }) : t("downloads.nothingToRetry"));
      void queryClient.invalidateQueries({ queryKey: queryKeys.downloads() });
      void queryClient.invalidateQueries({ queryKey: queryKeys.downloadsSummary });
    },
    onError: () => toast.error(t("downloads.retryFailed")),
  });
}
