import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { Lock } from "lucide-react";
import { Button } from "@/components/ui/button";
import { TrackList } from "@/components/tracks/TrackList";
import { useAuth } from "@/contexts/AuthContext";
import { useSignInSearch } from "@/hooks/useSignInSearch";
import { getTrendingTracks } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { useI18n } from "@/i18n";

const PREVIEWS = 8;

/**
 * The guest showcase on the Search page: searching is for signed-in users
 * (the `search` capability), so instead of a request that would 401 the
 * page invites to sign in and offers the trending previews.
 */
export function SignInToSearch() {
  const { authConfig } = useAuth();
  const { t } = useI18n();
  const back = useSignInSearch();
  const { data } = useQuery({
    queryKey: queryKeys.discover("trending-tracks"),
    queryFn: () => getTrendingTracks(20),
    staleTime: 10 * 60 * 1000,
  });
  const previews = (data?.items ?? []).filter((track) => track.preview_url).slice(0, PREVIEWS);

  return (
    <div className="space-y-8">
      <section className="flex flex-col items-start gap-3 rounded-lg border bg-card p-6">
        <Lock aria-hidden className="size-6 text-primary" />
        <h1 className="font-display text-2xl font-bold">{t("search.signInTitle")}</h1>
        <p className="max-w-prose text-muted-foreground">{t("search.signInBody")}</p>
        <div className="flex gap-2">
          <Button asChild>
            <Link to="/login" search={back}>
              {t("nav.signIn")}
            </Link>
          </Button>
          {authConfig?.registration_open && (
            <Button asChild variant="outline">
              <Link to="/register" search={back}>
                {t("search.register")}
              </Link>
            </Button>
          )}
        </div>
      </section>
      {previews.length > 0 && (
        <section className="space-y-3">
          <h2 className="font-display text-lg font-semibold">{t("discover.trendingTracks")}</h2>
          <TrackList tracks={previews} />
        </section>
      )}
    </div>
  );
}
