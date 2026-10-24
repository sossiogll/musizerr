import { useEffect, useState } from "react";
import { Disc3, ListMusic, Music, User } from "lucide-react";
import { cn } from "@/lib/utils";

type CoverType = "track" | "album" | "artist" | "playlist";

interface CoverImageProps {
  src: string | null | undefined;
  alt: string;
  type?: CoverType;
  size?: "sm" | "md" | "lg";
  className?: string;
}

const sizeClasses = {
  sm: "w-10 h-10",
  md: "w-12 h-12",
  lg: "w-20 h-20",
} as const;

const iconSizes = {
  sm: "w-4 h-4",
  md: "w-5 h-5",
  lg: "w-8 h-8",
} as const;

const fallbackIcon: Record<CoverType, typeof Music> = {
  track: Music,
  album: Disc3,
  artist: User,
  playlist: ListMusic,
};

export function CoverImage({
  src,
  alt,
  type = "track",
  size = "md",
  className,
}: CoverImageProps) {
  const Icon = fallbackIcon[type];
  // Se l'immagine non carica (URL scaduto, proxy non raggiungibile, 404…),
  // mostriamo il placeholder invece dell'icona di "immagine rotta" del browser.
  const [failed, setFailed] = useState(false);

  // Nuova src → riprova a caricare (evita di restare bloccati sul fallback).
  useEffect(() => setFailed(false), [src]);

  const showImage = !!src && !failed;

  return (
    <div
      className={cn(
        "flex-shrink-0 bg-muted flex items-center justify-center overflow-hidden",
        type === "artist" ? "rounded-full" : "rounded-sm",
        sizeClasses[size],
        className,
      )}
    >
      {showImage ? (
        <img
          src={src}
          alt={alt}
          loading="lazy"
          className="w-full h-full object-cover"
          onError={() => setFailed(true)}
        />
      ) : (
        <Icon className={cn("text-muted-foreground", iconSizes[size])} />
      )}
    </div>
  );
}
