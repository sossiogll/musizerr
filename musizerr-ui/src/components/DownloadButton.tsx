import type { ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { DownloadMode } from "@/hooks/useTrackDownload";
import { cn } from "@/lib/utils";

export interface DownloadButtonProps {
  /** The downloads the viewer may start, primary first (see downloadModes). */
  modes: DownloadMode[];
  onDownload: (mode: DownloadMode) => void;
  icon: ReactNode;
  /** The visible word(s) of the primary action. */
  text: string;
  /** Accessible name and tooltip of each mode: says exactly what it does. */
  labels: Record<DownloadMode, string>;
  /** Shorter text of the menu items (e.g. in a track row, where the menu is next to the title). */
  menuTexts?: Record<DownloadMode, string>;
  /** Accessible name of the menu trigger (admins' second action). */
  menuLabel: string;
  disabled?: boolean;
  /** Why it is disabled, as its tooltip. */
  title?: string;
  /** Class of the primary button. */
  className?: string;
  /** Class of the menu trigger: the chevron, and in `compact` mode the whole button on small screens. */
  menuClassName?: string;
  /** Text hidden on small screens: there the whole thing is one icon that opens the menu. */
  compact?: boolean;
  /** One icon on every tier (e.g. in the player): the action itself, or with two the menu of both. */
  iconOnly?: boolean;
}

/**
 * Every button does what it says. A user has one download: "Download",
 * whose name and tooltip say it also adds the track to the favorites. An
 * admin has a split button: the primary downloads only, the menu adds
 * "Download and add to favorites". In `compact` mode (a track row on a
 * phone) there is room for one icon: with two actions it opens the menu;
 * `iconOnly` is that on every tier.
 */
export function DownloadButton({
  modes,
  onDownload,
  icon,
  text,
  labels,
  menuTexts = labels,
  menuLabel,
  disabled,
  title,
  className,
  menuClassName,
  compact,
  iconOnly,
}: DownloadButtonProps) {
  const [primary, ...others] = modes;
  if (!primary) return null;

  if (iconOnly && others.length > 0) {
    return (
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label={menuLabel}
            title={title ?? menuLabel}
            disabled={disabled}
            onClick={(e) => e.stopPropagation()}
            className={menuClassName ?? className}
          >
            {icon}
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" side="top">
          {modes.map((mode) => (
            <DropdownMenuItem key={mode} onSelect={() => onDownload(mode)}>
              {menuTexts[mode]}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    );
  }

  const primaryButton = (extra?: string) => (
    <button
      type="button"
      aria-label={labels[primary]}
      title={title ?? labels[primary]}
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation();
        onDownload(primary);
      }}
      className={cn(className, extra)}
    >
      {icon}
      <span className={cn("truncate", compact && "max-md:sr-only", iconOnly && "sr-only")}>{text}</span>
    </button>
  );

  if (others.length === 0) return primaryButton();

  return (
    <span className="inline-flex items-center">
      {primaryButton(compact ? "max-md:hidden md:rounded-r-none md:px-2.5" : "rounded-r-none")}
      <DropdownMenu>
        {/* One trigger on every tier: in `compact` mode it is the whole button
            on small screens (the icon) and the chevron half of the split
            button from M up; `menuClassName` sizes it per tier. */}
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label={menuLabel}
            title={title ?? menuLabel}
            disabled={disabled}
            onClick={(e) => e.stopPropagation()}
            className={menuClassName}
          >
            {compact && <span className="contents md:hidden">{icon}</span>}
            <ChevronDown aria-hidden className={compact ? "size-3.5 max-md:hidden" : "size-4"} />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          {compact && (
            <DropdownMenuItem className="max-md:flex md:hidden" onSelect={() => onDownload(primary)}>
              {menuTexts[primary]}
            </DropdownMenuItem>
          )}
          {others.map((mode) => (
            <DropdownMenuItem key={mode} onSelect={() => onDownload(mode)}>
              {menuTexts[mode]}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </span>
  );
}
