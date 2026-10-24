import { forwardRef } from "react";
import { createLink } from "@tanstack/react-router";
import { tabItemClass, tabStripClass } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";

/**
 * Section tabs that are routes (Administration, Settings): a labelled nav
 * of links in the app's tab look. The router marks the current one
 * (aria-current="page"), which is what underlines it.
 *
 *   <TabNav label="Sections">
 *     <TabLink to="/admin/users">Users</TabLink>
 *   </TabNav>
 */
export function TabNav({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <nav aria-label={label}>
      <ul className={tabStripClass}>{children}</ul>
    </nav>
  );
}

const TabAnchor = forwardRef<HTMLAnchorElement, React.AnchorHTMLAttributes<HTMLAnchorElement>>(function TabAnchor({ className, ...props }, ref) {
  return (
    <li className="flex">
      <a ref={ref} className={cn(tabItemClass, className)} {...props} />
    </li>
  );
});

/** A typed router link rendered as one tab of a TabNav. */
export const TabLink = createLink(TabAnchor);
