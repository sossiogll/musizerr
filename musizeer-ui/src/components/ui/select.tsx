import * as React from "react";
import { cn } from "@/lib/utils";

/** The app's native select: 44px on touch screens (S/XS), denser from M up. */
const Select = React.forwardRef<HTMLSelectElement, React.ComponentProps<"select">>(({ className, ...props }, ref) => (
  <select
    ref={ref}
    className={cn(
      "h-11 rounded-lg border bg-background px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 sm:h-9",
      className,
    )}
    {...props}
  />
));
Select.displayName = "Select";

export { Select };
