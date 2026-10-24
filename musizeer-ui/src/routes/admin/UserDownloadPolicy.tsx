import { useId, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Loader2, RotateCcw } from "lucide-react";
import { clearUserDownloadPolicy, getDownloadPolicy, getUserDownloadPolicy, setUserDownloadPolicy } from "@/lib/api-client";
import { REQUEST_KINDS, REQUEST_MODES } from "@/lib/download-requests";
import { useFailureToast } from "@/lib/error-text";
import { queryKeys } from "@/lib/query-keys";
import { ApiErrorMessage } from "@/components/ErrorMessage";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useRequestText } from "@/hooks/useRequestText";
import { useServerAction } from "@/hooks/useServerAction";
import { useI18n } from "@/i18n";
import type { DownloadPolicy, RequestKind, RequestMode, User, UserDownloadPolicyOverride, UserDownloadPolicyView } from "@/types";

/** What the form holds: a mode and a threshold per kind and the quota, as typed (empty = follow the general policy). */
interface Draft {
  rules: Partial<Record<RequestKind, { mode: RequestMode | ""; threshold: string }>>;
  quota: string;
}

const EMPTY: Draft = { rules: {}, quota: "" };

const draftOf = (override: UserDownloadPolicyOverride): Draft => ({
  rules: Object.fromEntries(
    REQUEST_KINDS.flatMap((kind) => {
      const rule = override.rules[kind];
      return rule ? [[kind, { mode: rule.mode ?? "", threshold: rule.threshold?.toString() ?? "" }]] : [];
    }),
  ),
  quota: override.quota_tracks?.toString() ?? "",
});

/** The override a draft asks for: a field left empty inherits (null), a kind with nothing set is left out. */
const overrideOf = (draft: Draft): UserDownloadPolicyOverride => ({
  rules: Object.fromEntries(
    REQUEST_KINDS.flatMap((kind) => {
      const rule = draft.rules[kind];
      if (!rule) return [];
      const mode = rule.mode || null;
      const threshold = rule.threshold === "" ? null : Number(rule.threshold);
      return mode || threshold !== null ? [[kind, { mode, threshold }]] : [];
    }),
  ),
  quota_tracks: draft.quota === "" ? null : Number(draft.quota),
});

const isWhole = (value: string, floor: number) => /^\d+$/.test(value) && Number(value) >= floor;

/** Whether what was typed can be sent: a threshold of at least 1 tracks, a quota of 0 or more. */
const valid = (draft: Draft) =>
  Object.values(draft.rules).every((rule) => !rule || rule.threshold === "" || isWhole(rule.threshold, 1)) && (draft.quota === "" || isWhole(draft.quota, 0));

/**
 * A user's download policy, in Administration › Users: for each kind of
 * request, follow the general policy or take another mode (and, for "up to",
 * another threshold), plus a quota of their own (0 = none). What the server
 * says results from it is shown under each kind. Only an admin can write it.
 */
export function UserDownloadPolicyPanel({ user }: { user: User }) {
  const { t } = useI18n();
  const titleId = useId();
  const view = useQuery({ queryKey: queryKeys.userDownloadPolicy(user.id), queryFn: () => getUserDownloadPolicy(user.id) });
  const general = useQuery({ queryKey: queryKeys.downloadPolicy, queryFn: getDownloadPolicy });
  const error = view.error ?? general.error;

  return (
    <section aria-labelledby={titleId} className="space-y-4 border-t bg-muted/30 px-4 py-4">
      <div className="space-y-1">
        <h3 id={titleId} className="font-display text-base font-semibold">
          {t("userPolicy.title", { name: user.username })}
        </h3>
        <p className="max-w-prose text-sm text-muted-foreground">{t("userPolicy.intro")}</p>
      </div>
      {view.data && general.data ? (
        <PolicyForm userId={user.id} view={view.data} general={general.data} />
      ) : error ? (
        <ApiErrorMessage className="text-sm text-destructive" error={error} fallback={t("userPolicy.loadFailed")} />
      ) : (
        <Skeleton aria-hidden className="h-40" />
      )}
    </section>
  );
}

function PolicyForm({ userId, view, general }: { userId: string; view: UserDownloadPolicyView; general: DownloadPolicy }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const server = useServerAction();
  const failed = useFailureToast();
  const stored = useMemo(() => draftOf(view.override), [view.override]);
  const [draft, setDraft] = useState<Draft>(stored);
  const kinds = REQUEST_KINDS.filter((kind) => kind in general.rules);

  const written = (next: UserDownloadPolicyView, message: string) => {
    queryClient.setQueryData(queryKeys.userDownloadPolicy(userId), next);
    toast.success(message);
  };
  const save = useMutation({
    mutationFn: () => setUserDownloadPolicy(userId, overrideOf(draft)),
    onSuccess: (next) => written(next, t("userPolicy.saved")),
    onError: (error) => failed(error, t("userPolicy.saveFailed")),
  });
  const reset = useMutation({
    mutationFn: () => clearUserDownloadPolicy(userId),
    onSuccess: (next) => {
      setDraft(EMPTY);
      written(next, t("userPolicy.cleared"));
    },
    onError: (error) => failed(error, t("userPolicy.saveFailed")),
  });

  const dirty = JSON.stringify(overrideOf(draft)) !== JSON.stringify(overrideOf(stored));
  const customized = JSON.stringify(overrideOf(stored)) !== JSON.stringify(overrideOf(EMPTY));
  const busy = save.isPending || reset.isPending || server.disabled;

  const change = (kind: RequestKind, rule: { mode: RequestMode | ""; threshold: string }) =>
    setDraft((current) => ({ ...current, rules: { ...current.rules, [kind]: rule } }));

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <ul className="space-y-3">
        {kinds.map((kind) => (
          <li key={kind}>
            <KindRule
              kind={kind}
              general={general.rules[kind]}
              effective={view.effective.rules[kind]}
              value={draft.rules[kind] ?? { mode: "", threshold: "" }}
              onChange={(rule) => change(kind, rule)}
            />
          </li>
        ))}
      </ul>

      <div className="grid gap-1.5 sm:grid-cols-[minmax(0,12rem)_minmax(0,1fr)] sm:items-start sm:gap-3">
        <label htmlFor={`quota-${userId}`} className="text-sm font-medium">
          {t("userPolicy.quota", { days: general.quota.window_days })}
        </label>
        <div className="space-y-1">
          <Input
            id={`quota-${userId}`}
            type="number"
            min={0}
            inputMode="numeric"
            className="h-11 w-32 sm:h-9"
            placeholder={String(general.quota.tracks)}
            value={draft.quota}
            onChange={(e) => setDraft((current) => ({ ...current, quota: e.target.value }))}
          />
          <p className="text-xs text-muted-foreground">
            {t("userPolicy.quotaHint", { tracks: general.quota.tracks })}
            {" · "}
            {view.effective.quota.tracks === 0 ? t("userPolicy.quotaNone") : t("userPolicy.quotaNow", { count: view.effective.quota.tracks })}
          </p>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={busy || !dirty || !valid(draft)} title={server.reason}>
          {save.isPending && <Loader2 aria-hidden className="animate-spin" />}
          {t("common.save")}
        </Button>
        <Button type="button" variant="outline" disabled={busy || !customized} title={server.reason} onClick={() => reset.mutate()}>
          {reset.isPending ? <Loader2 aria-hidden className="animate-spin" /> : <RotateCcw aria-hidden />}
          {t("userPolicy.reset")}
        </Button>
      </div>
    </form>
  );
}

/** One kind of request: follow the general rule, or a mode of its own (and a threshold for "up to"). */
function KindRule({
  kind,
  general,
  effective,
  value,
  onChange,
}: {
  kind: RequestKind;
  general: { mode: RequestMode; threshold: number };
  effective: { mode: RequestMode; threshold: number };
  value: { mode: RequestMode | ""; threshold: string };
  onChange: (rule: { mode: RequestMode | ""; threshold: string }) => void;
}) {
  const { t } = useI18n();
  const text = useRequestText();
  const label = text.kind(kind);
  // The mode that applies if this is saved; a threshold only means something for "up to".
  const resulting = value.mode || general.mode;
  return (
    <div className="grid gap-1.5 sm:grid-cols-[minmax(0,12rem)_minmax(0,1fr)] sm:items-start sm:gap-3">
      <div>
        <p className="text-sm font-medium">{label}</p>
        <p className="text-xs text-muted-foreground">{t("userPolicy.now", { rule: text.rule(effective) })}</p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Select
          aria-label={t("userPolicy.modeFor", { kind: label })}
          value={value.mode}
          onChange={(e) => {
            const mode = e.target.value as RequestMode | "";
            onChange({ mode, threshold: (mode || general.mode) === "auto_up_to" ? value.threshold : "" });
          }}
        >
          <option value="">{t("userPolicy.inherit", { rule: text.rule(general) })}</option>
          {REQUEST_MODES.map((mode) => (
            <option key={mode} value={mode}>
              {text.mode(mode)}
            </option>
          ))}
        </Select>
        {resulting === "auto_up_to" && (
          <Input
            type="number"
            min={1}
            inputMode="numeric"
            aria-label={t("userPolicy.thresholdFor", { kind: label })}
            className="h-11 w-28 sm:h-9"
            placeholder={String(general.threshold)}
            value={value.threshold}
            onChange={(e) => onChange({ ...value, threshold: e.target.value })}
          />
        )}
      </div>
    </div>
  );
}
