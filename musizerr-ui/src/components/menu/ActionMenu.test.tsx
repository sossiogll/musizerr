import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { I18nProvider } from "@/i18n";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { ActionMenu, LONG_PRESS_MS, tidy, type MenuEntries } from "./ActionMenu";

// The REAL menu (Radix) behind every "⋯", right click and long press: one
// menu from three ways in, with the keyboard and focus a menu owes.

// jsdom has no PointerEvent: without one `pointerType` and the coordinates are lost.
class FakePointerEvent extends MouseEvent {
  pointerType: string;
  constructor(type: string, init: MouseEventInit & { pointerType?: string } = {}) {
    super(type, init);
    this.pointerType = init.pointerType ?? "";
  }
}
beforeAll(() => void vi.stubGlobal("PointerEvent", FakePointerEvent));
afterAll(() => void vi.unstubAllGlobals());

const onPlay = vi.fn();
const onRemove = vi.fn();
const onRowClick = vi.fn();

function entries(): MenuEntries {
  return [
    { type: "item", id: "play", label: "Play", onSelect: onPlay },
    { type: "separator" },
    { type: "item", id: "locked", label: "Locked", onSelect: vi.fn(), disabled: true },
    { type: "submenu", id: "more", label: "Add to playlist", content: <DropdownMenuItem>Road trip</DropdownMenuItem> },
    { type: "separator" },
    { type: "item", id: "remove", label: "Remove", onSelect: onRemove, destructive: true },
  ];
}

function renderMenu(list: MenuEntries = entries()) {
  return render(
    <I18nProvider>
      <ActionMenu entries={list} label="More actions for Song A" heading="Song A" buttonClassName="extra">
        {({ surface, button }) => (
          <div data-testid="row" {...surface} className={surface.className} onClick={onRowClick} onClickCapture={surface.onClickCapture}>
            <a href="#song" data-testid="link">
              Song A
            </a>
            {button}
          </div>
        )}
      </ActionMenu>
    </I18nProvider>,
  );
}

const button = () => screen.getByRole("button", { name: "More actions for Song A" });
const itemNames = () => screen.getAllByRole("menuitem").map((i) => i.textContent);

beforeEach(() => {
  onPlay.mockReset();
  onRemove.mockReset();
  onRowClick.mockReset();
});

afterEach(() => vi.useRealTimers());

describe("tidy", () => {
  it("drops what is not there and the dividers that divide nothing", () => {
    const item = { type: "item" as const, id: "a", label: "A", onSelect: () => {} };
    const result = tidy([{ type: "separator" }, false, item, { type: "separator" }, null, { type: "separator" }, { ...item, id: "b" }, undefined, { type: "separator" }]);
    expect(result.map((e) => e.type)).toEqual(["item", "separator", "item"]);
  });
});

describe("ActionMenu — the button", () => {
  it("is one menu button, named, that keeps the owner's classes", () => {
    renderMenu();
    expect(screen.getAllByRole("button", { name: "More actions for Song A" })).toHaveLength(1);
    expect(button()).toHaveAttribute("aria-haspopup", "menu");
    expect(button()).toHaveAttribute("aria-expanded", "false");
    expect(button()).toHaveAttribute("title", "More actions for Song A");
    expect(button().className).toEqual(expect.stringContaining("size-11"));
    expect(button().className).toEqual(expect.stringContaining("extra"));
  });

  it("opens a named menu with the heading and the entries in order, dividers included", async () => {
    renderMenu();
    const trigger = button();
    fireEvent.click(trigger);

    const menu = await screen.findByRole("menu", { name: "More actions for Song A" });
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(menu).toHaveTextContent("Song A");
    expect(itemNames()).toEqual(["Play", "Locked", "Add to playlist", "Remove"]);
    expect(screen.getAllByRole("separator")).toHaveLength(2);
  });

  it("does not click through to the row under it", async () => {
    renderMenu();
    fireEvent.click(button());
    await screen.findByRole("menu");
    expect(onRowClick).not.toHaveBeenCalled();
  });

  it("anchors the menu under the button, aligned to its end", async () => {
    renderMenu();
    button().getBoundingClientRect = () => ({ left: 300, right: 344, top: 10, bottom: 54, width: 44, height: 44, x: 300, y: 10, toJSON: () => ({}) });
    fireEvent.click(button());
    await screen.findByRole("menu");
    const anchor = document.body.querySelector<HTMLElement>("span.fixed[aria-label]")!;
    expect([anchor.style.left, anchor.style.top]).toEqual(["344px", "54px"]);
  });

  it("with nothing to offer says so instead of an empty box", async () => {
    renderMenu([false, { type: "separator" }]);
    fireEvent.click(button());
    await screen.findByRole("menu");
    expect(screen.getByText("Nothing to do here")).toBeInTheDocument();
    expect(screen.queryAllByRole("menuitem")).toHaveLength(0);
  });
});

describe("ActionMenu — keyboard and focus", () => {
  it("walks the entries with the arrow keys, skips the disabled one and selects with Enter", async () => {
    renderMenu();
    button().focus();
    fireEvent.keyDown(button(), { key: "Enter" });
    fireEvent.click(button());
    const menu = await screen.findByRole("menu");

    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(screen.getByRole("menuitem", { name: "Play" })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
    // "Locked" is disabled: Radix keeps it in the order but never selects it.
    fireEvent.keyDown(document.activeElement!, { key: "ArrowUp" });
    expect(screen.getByRole("menuitem", { name: "Play" })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: "Enter" });

    expect(onPlay).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
  });

  it("closes with Escape and gives the focus back to the button", async () => {
    renderMenu();
    button().focus();
    fireEvent.click(button());
    const menu = await screen.findByRole("menu");
    fireEvent.keyDown(menu, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    expect(button()).toHaveFocus();
    expect(button()).toHaveAttribute("aria-expanded", "false");
  });

  it("a disabled entry is not selected", async () => {
    renderMenu();
    fireEvent.click(button());
    const locked = await screen.findByRole("menuitem", { name: "Locked" });
    expect(locked).toHaveAttribute("aria-disabled", "true");
  });

  it("opens a submenu with the right arrow", async () => {
    renderMenu();
    fireEvent.click(button());
    const sub = await screen.findByRole("menuitem", { name: "Add to playlist" });
    expect(sub).toHaveAttribute("aria-haspopup", "menu");
    sub.focus();
    fireEvent.keyDown(sub, { key: "ArrowRight" });
    expect(await screen.findByRole("menuitem", { name: "Road trip" })).toBeInTheDocument();
  });

  it("marks a destructive entry", async () => {
    renderMenu();
    fireEvent.click(button());
    expect((await screen.findByRole("menuitem", { name: "Remove" })).className).toEqual(expect.stringContaining("text-destructive"));
  });
});

describe("ActionMenu — right click", () => {
  it("opens the same menu where the pointer is, and keeps the browser's own menu away", async () => {
    renderMenu();
    const notPrevented = fireEvent.contextMenu(screen.getByTestId("link"), { clientX: 120, clientY: 80 });

    expect(notPrevented).toBe(false);
    await screen.findByRole("menu", { name: "More actions for Song A" });
    expect(itemNames()).toEqual(["Play", "Locked", "Add to playlist", "Remove"]);
    const anchor = document.body.querySelector<HTMLElement>("span.fixed[aria-label]")!;
    expect([anchor.style.left, anchor.style.top]).toEqual(["120px", "80px"]);
  });

  it("gives the focus back to what had it", async () => {
    renderMenu();
    const link = screen.getByTestId("link");
    link.focus();
    fireEvent.contextMenu(link, { clientX: 5, clientY: 5 });
    const menu = await screen.findByRole("menu");
    fireEvent.keyDown(menu, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    expect(link).toHaveFocus();
  });

  it("from the keyboard (no pointer position) opens under the row", async () => {
    renderMenu();
    screen.getByTestId("row").getBoundingClientRect = () => ({ left: 40, right: 400, top: 200, bottom: 256, width: 360, height: 56, x: 40, y: 200, toJSON: () => ({}) });
    fireEvent.contextMenu(screen.getByTestId("row"), { clientX: 0, clientY: 0 });
    await screen.findByRole("menu");
    const anchor = document.body.querySelector<HTMLElement>("span.fixed[aria-label]")!;
    expect([anchor.style.left, anchor.style.top]).toEqual(["56px", "228px"]);
  });
});

describe("ActionMenu — long press", () => {
  const touch = (type: "pointerDown" | "pointerMove" | "pointerUp", target: Element, x = 50, y = 60, pointerType = "touch") =>
    fireEvent[type](target, { pointerType, clientX: x, clientY: y, button: 0 });

  it("opens the menu after a press that stays put, and swallows the click that ends it", async () => {
    vi.useFakeTimers();
    renderMenu();
    const link = screen.getByTestId("link");
    touch("pointerDown", link);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    act(() => void vi.advanceTimersByTime(LONG_PRESS_MS));
    vi.useRealTimers();

    await screen.findByRole("menu", { name: "More actions for Song A" });
    const anchor = document.body.querySelector<HTMLElement>("span.fixed[aria-label]")!;
    expect([anchor.style.left, anchor.style.top]).toEqual(["50px", "60px"]);
    touch("pointerUp", link);
    // The finger lifting would follow the link: it does not.
    expect(fireEvent.click(link)).toBe(false);
    expect(onRowClick).not.toHaveBeenCalled();
  });

  it("a tap is still a tap: no menu, and the click goes through", () => {
    vi.useFakeTimers();
    renderMenu();
    const link = screen.getByTestId("link");
    touch("pointerDown", link);
    act(() => void vi.advanceTimersByTime(LONG_PRESS_MS - 50));
    touch("pointerUp", link);
    act(() => void vi.advanceTimersByTime(LONG_PRESS_MS));

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    link.addEventListener("click", (e) => e.preventDefault());
    fireEvent.click(link);
    expect(onRowClick).toHaveBeenCalledTimes(1);
  });

  it("a drift past a few pixels is a scroll, not a press", () => {
    vi.useFakeTimers();
    renderMenu();
    const link = screen.getByTestId("link");
    touch("pointerDown", link, 50, 60);
    touch("pointerMove", link, 52, 63);
    touch("pointerMove", link, 50, 80);
    act(() => void vi.advanceTimersByTime(LONG_PRESS_MS * 2));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("a mouse press does not open it (the right click does)", () => {
    vi.useFakeTimers();
    renderMenu();
    touch("pointerDown", screen.getByTestId("link"), 50, 60, "mouse");
    act(() => void vi.advanceTimersByTime(LONG_PRESS_MS * 2));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("the browser taking the touch over (a scroll: pointercancel) cancels it", () => {
    vi.useFakeTimers();
    renderMenu();
    const link = screen.getByTestId("link");
    touch("pointerDown", link);
    fireEvent.pointerCancel(link);
    act(() => void vi.advanceTimersByTime(LONG_PRESS_MS * 2));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("keeps a touch screen from selecting the text it presses and from the system callout", () => {
    renderMenu();
    const classes = screen.getByTestId("row").className.split(/\s+/);
    expect(classes).toEqual(expect.arrayContaining(["[@media(hover:none)]:select-none", "[@media(hover:none)]:[-webkit-touch-callout:none]"]));
  });
});
