import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Tabs, TabsContent, TabsList, TabsTrigger, tabItemClass, tabStripClass } from "./tabs";

function InPage() {
  return (
    <Tabs defaultValue="a">
      <TabsList aria-label="Sections">
        <TabsTrigger value="a">Alpha</TabsTrigger>
        <TabsTrigger value="b">Beta</TabsTrigger>
        <TabsTrigger value="c">Gamma</TabsTrigger>
      </TabsList>
      <TabsContent value="a">Panel A</TabsContent>
      <TabsContent value="b">Panel B</TabsContent>
      <TabsContent value="c">Panel C</TabsContent>
    </Tabs>
  );
}

describe("in-page tabs", () => {
  it("keep the tab semantics and move with the arrow keys", async () => {
    render(<InPage />);
    const [alpha, beta, gamma] = screen.getAllByRole("tab");
    expect(screen.getByRole("tablist", { name: "Sections" })).toBeInTheDocument();
    expect(alpha).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Panel A");

    alpha.focus();
    fireEvent.keyDown(alpha, { key: "ArrowRight" });
    // Radix moves the focus on the next task.
    await waitFor(() => expect(beta).toHaveFocus());
    expect(beta).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Panel B");

    fireEvent.keyDown(beta, { key: "End" });
    await waitFor(() => expect(gamma).toHaveFocus());
    fireEvent.keyDown(gamma, { key: "ArrowRight" });
    await waitFor(() => expect(alpha).toHaveFocus());
  });

  it("are drawn as the underlined strip, not the old segmented control", () => {
    render(<InPage />);
    const list = screen.getByRole("tablist");
    for (const cls of tabStripClass.split(" ")) expect(list).toHaveClass(cls);
    for (const tab of screen.getAllByRole("tab")) for (const cls of tabItemClass.split(" ")) expect(tab).toHaveClass(cls);
    // The segmented look: a muted pill track with raised, shadowed triggers.
    expect(list.className).not.toMatch(/\bbg-muted\b|\bp-1\b/);
    for (const tab of screen.getAllByRole("tab")) expect(tab.className).not.toMatch(/data-\[state=active\]:(bg-background|shadow)/);
  });

  it("the underline follows the current item however it is marked", () => {
    for (const mark of ["aria-selected", "aria-[current=page]", "aria-pressed"]) {
      expect(tabItemClass).toContain(`${mark}:border-primary`);
    }
    // 44px targets and sideways scrolling on narrow screens.
    expect(tabItemClass).toContain("h-11");
    expect(tabStripClass).toContain("overflow-x-auto");
  });
});
