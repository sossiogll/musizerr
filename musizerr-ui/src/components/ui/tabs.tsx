import * as React from "react";
import * as TabsPrimitive from "@radix-ui/react-tabs";
import { cn } from "@/lib/utils";

/**
 * The app's one tab look: labels on a bottom rule, the current one
 * underlined in the brand color. Shared by in-page tabs (Radix, below),
 * route tabs (TabNav) and filter strips (FilterTabs); each marks its current
 * item its own way (aria-selected, aria-current, aria-pressed) and the
 * classes follow whichever it uses.
 *
 * The strip scrolls sideways when it does not fit. The rule is an inset
 * shadow of the strip itself, so it spans the whole strip at any scroll
 * position and the 2px underline sits on it without being clipped.
 */
export const tabStripClass =
  "flex max-w-full gap-1 overflow-x-auto overscroll-x-contain shadow-[inset_0_-1px_0_var(--color-border)] [scrollbar-width:none]";

/** 44px tall at every tier (touch targets on S/XS); the underline is the primary color, ≥3:1 on the background in both themes. */
export const tabItemClass = cn(
  "inline-flex h-11 shrink-0 items-center gap-2 rounded-t-sm border-b-2 border-transparent px-3 text-sm font-medium whitespace-nowrap text-muted-foreground transition-colors",
  "hover:border-border hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50",
  "aria-selected:border-primary aria-selected:text-foreground aria-[current=page]:border-primary aria-[current=page]:text-foreground aria-pressed:border-primary aria-pressed:text-foreground",
);

const Tabs = TabsPrimitive.Root;

const TabsList = React.forwardRef<
  React.ComponentRef<typeof TabsPrimitive.List>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>
>(({ className, ...props }, ref) => <TabsPrimitive.List ref={ref} className={cn(tabStripClass, className)} {...props} />);
TabsList.displayName = TabsPrimitive.List.displayName;

const TabsTrigger = React.forwardRef<
  React.ComponentRef<typeof TabsPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>
>(({ className, ...props }, ref) => <TabsPrimitive.Trigger ref={ref} className={cn(tabItemClass, className)} {...props} />);
TabsTrigger.displayName = TabsPrimitive.Trigger.displayName;

const TabsContent = React.forwardRef<
  React.ComponentRef<typeof TabsPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Content>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Content
    ref={ref}
    className={cn("mt-6 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background", className)}
    {...props}
  />
));
TabsContent.displayName = TabsPrimitive.Content.displayName;

export { Tabs, TabsList, TabsTrigger, TabsContent };
