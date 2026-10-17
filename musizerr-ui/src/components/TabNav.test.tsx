import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";
import { renderPage } from "@/test/render";
import { AdminLayout } from "@/routes/admin/layout";
import { FilterTabs } from "./FilterTabs";
import { tabItemClass, tabStripClass } from "./ui/tabs";

const hasClasses = (el: Element, classes: string) => classes.split(" ").every((c) => el.classList.contains(c));

describe("TabNav (route tabs)", () => {
  it("Administration: section links in the shared tab look, the current one marked", async () => {
    const { router } = renderPage(AdminLayout, { path: "/admin/users", otherPaths: ["/admin/requests", "/admin/providers", "/admin/routines", "/admin/sources", "/admin/duplicates", "/admin/system"] });
    const nav = await screen.findByRole("navigation", { name: "Administration sections" });
    const links = within(nav).getAllByRole("link");
    expect(links.map((l) => l.textContent)).toEqual(["Users", "Requests", "Providers", "Routines", "Sources", "Duplicates", "System"]);
    expect(within(nav).getByRole("list")).toHaveClass(...tabStripClass.split(" "));
    for (const link of links) expect(hasClasses(link, tabItemClass)).toBe(true);
    expect(within(nav).getByRole("link", { name: "Users" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Routines" })).not.toHaveAttribute("aria-current");

    fireEvent.click(within(nav).getByRole("link", { name: "Routines" }));
    await vi.waitFor(() => expect(router.state.location.pathname).toBe("/admin/routines"));
  });
});

describe("FilterTabs", () => {
  it("filters are toggle buttons in the same underlined look, the count next to the label", async () => {
    const onChange = vi.fn();
    renderPage(
      () => (
        <FilterTabs
          label="Filter by state"
          value="all"
          onChange={onChange}
          tabs={[
            { value: "all", label: "All", count: 12 },
            { value: "failed", label: "Failed", count: 2 },
          ]}
        />
      ),
      { path: "/" },
    );
    const group = await screen.findByRole("group", { name: "Filter by state" });
    expect(group).toHaveClass(...tabStripClass.split(" "));
    // The count is a span after the label: whether jsdom puts a space between the two depends on its default display of a span, the browser's on the flex gap.
    const all = within(group).getByRole("button", { name: /^All\s?12$/ });
    const failed = within(group).getByRole("button", { name: /^Failed\s?2$/ });
    expect(all).toHaveAttribute("aria-pressed", "true");
    expect(failed).toHaveAttribute("aria-pressed", "false");
    expect(hasClasses(failed, tabItemClass)).toBe(true);
    fireEvent.click(failed);
    expect(onChange).toHaveBeenCalledWith("failed");
  });
});
