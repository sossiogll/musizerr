/**
 * Datetimes from the API. The backend sends them in UTC with their offset
 * ("…Z" or "…+00:00"); a string without one (an older backend, data written
 * before the fix) is UTC too, never local time — `new Date()` would read it
 * as local and shift it by the viewer's offset.
 */
const HAS_ZONE = /(?:Z|[+-]\d{2}:?\d{2})$/i;
const HAS_TIME = /T\d{2}:\d{2}/;

export function parseApiDate(value: string): Date {
  return new Date(HAS_TIME.test(value) && !HAS_ZONE.test(value) ? `${value}Z` : value);
}

/** "3 minutes ago", "in 2 hours": the largest unit that fits, rounded. */
export function relativeTime(value: string, locale: string, now: number = Date.now()): string {
  const diffSec = Math.round((parseApiDate(value).getTime() - now) / 1000);
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  if (Math.abs(diffSec) < 60) return rtf.format(diffSec, "second");
  const diffMin = Math.round(diffSec / 60);
  if (Math.abs(diffMin) < 60) return rtf.format(diffMin, "minute");
  const diffHour = Math.round(diffMin / 60);
  if (Math.abs(diffHour) < 24) return rtf.format(diffHour, "hour");
  return rtf.format(Math.round(diffHour / 24), "day");
}

/** The date and time in the viewer's zone and locale. */
export function absoluteTime(value: string, locale: string): string {
  return parseApiDate(value).toLocaleString(locale);
}

/** A duration in seconds, short: "0.4 s", "12 s", "3 min 5 s", "2 h 10 min". */
export function formatDuration(seconds: number, locale: string): string {
  const number = (n: number, digits = 0) => n.toLocaleString(locale, { maximumFractionDigits: digits });
  if (seconds < 10) return `${number(seconds, 1)} s`;
  if (seconds < 60) return `${number(Math.round(seconds))} s`;
  const total = Math.round(seconds);
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return total % 60 ? `${minutes} min ${total % 60} s` : `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
}
