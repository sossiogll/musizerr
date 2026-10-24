/**
 * Animated backgrounds: the names the personal `background` setting can take
 * (core/settings_catalog.py, scope "user", declared default "off") and the
 * media queries that decide whether one may run at all.
 */

export const BACKGROUND_KINDS = ["aurora", "waves", "spectrum", "typography"] as const;
export type BackgroundKind = (typeof BACKGROUND_KINDS)[number];

/** The setting's value when it is missing or unknown: no background. */
export const BACKGROUND_OFF = "off";

export const BACKGROUND_SETTING_KEY = "background";

/**
 * Where a background may run: a wide viewport driven by a precise pointer
 * that can hover (a mouse or a trackpad). A phone, a tablet, a touch laptop
 * in tablet mode or a narrow window never matches, and the answer is
 * re-evaluated when the query changes. The width is the shell's `lg` tier.
 */
export const BACKGROUND_DESKTOP_QUERY = "(min-width: 1024px) and (hover: hover) and (pointer: fine)";

export const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";

/** The effect a stored value names, or null for "off", missing or unknown. */
export function backgroundKind(value: unknown): BackgroundKind | null {
  return (BACKGROUND_KINDS as readonly unknown[]).includes(value) ? (value as BackgroundKind) : null;
}
