import { tabItemClass, tabStripClass } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";

export interface FilterTab<V extends string> {
  value: V;
  label: string;
  count?: number;
}

/**
 * A strip of filters with counters (e.g. download states), in the app's tab
 * look: toggle buttons (aria-pressed) in a labelled group, the count next
 * to each label. Scrolls sideways on narrow screens instead of wrapping.
 */
export function FilterTabs<V extends string>({
  tabs,
  value,
  onChange,
  label,
}: {
  tabs: readonly FilterTab<V>[];
  value: V;
  onChange: (value: V) => void;
  /** What the tabs filter, for screen readers. */
  label: string;
}) {
  return (
    <div role="group" aria-label={label} className={tabStripClass}>
      {tabs.map((tab) => (
        <button key={tab.value} type="button" aria-pressed={tab.value === value} onClick={() => onChange(tab.value)} className={cn("group", tabItemClass)}>
          {tab.label}
          {tab.count !== undefined && (
            <span className="rounded-xs bg-muted px-1.5 text-xs tabular-nums text-muted-foreground group-aria-pressed:bg-primary/15 group-aria-pressed:text-primary">
              {tab.count}
            </span>
          )}
        </button>
      ))}
    </div>
  );
}
