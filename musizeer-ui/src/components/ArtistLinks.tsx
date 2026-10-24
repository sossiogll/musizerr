import { Fragment } from "react";
import { Link } from "@tanstack/react-router";
import type { ArtistRef } from "@/types";

/**
 * The artists of a track or an album, each linked to its page when the
 * provider gave its id; `fallback` (the plain artist string) otherwise.
 */
export function ArtistLinks({ artists, fallback, className }: { artists: ArtistRef[] | undefined; fallback: string; className?: string }) {
  if (!artists?.length) return <span className={className}>{fallback}</span>;
  return (
    <span className={className}>
      {artists.map((a, i) => (
        <Fragment key={`${a.provider}:${a.provider_id || a.name}`}>
          {i > 0 && ", "}
          {a.provider_id ? (
            <Link
              to="/artist/$provider/$id"
              params={{ provider: a.provider, id: a.provider_id }}
              className="rounded-xs font-medium text-foreground hover:underline focus-visible:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {a.name}
            </Link>
          ) : (
            a.name
          )}
        </Fragment>
      ))}
    </span>
  );
}
