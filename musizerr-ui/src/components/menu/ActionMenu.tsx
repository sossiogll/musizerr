import { useCallback, useEffect, useRef, useState, type HTMLAttributes, type MouseEvent, type PointerEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { MoreHorizontal } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";

/** What an entry of the menu is: an action, a divider, or a submenu with its own content. */
export type MenuEntry =
  | {
      type: "item";
      id: string;
      label: string;
      icon?: ReactNode;
      onSelect: () => void;
      disabled?: boolean;
      /** A tooltip: why it is disabled, or what it will do. */
      hint?: string;
      destructive?: boolean;
    }
  | { type: "separator" }
  | { type: "submenu"; id: string; label: string; icon?: ReactNode; content: ReactNode; disabled?: boolean };

/** Entries as the callers build them: a condition that fails is just `false`. */
export type MenuEntries = (MenuEntry | false | null | undefined)[];

/** Drops the entries that are not there and the dividers left with nothing to divide. */
export function tidy(entries: MenuEntries): MenuEntry[] {
  const result: MenuEntry[] = [];
  for (const entry of entries) {
    if (!entry) continue;
    if (entry.type === "separator" && (result.length === 0 || result[result.length - 1].type === "separator")) continue;
    result.push(entry);
  }
  if (result[result.length - 1]?.type === "separator") result.pop();
  return result;
}

/** How long a finger rests on a row to open its menu, and how far it may drift meanwhile. */
export const LONG_PRESS_MS = 500;
const LONG_PRESS_SLOP_PX = 10;
// The click that ends a long press must not follow the link or press the button under the finger.
const CLICK_AFTER_LONG_PRESS_MS = 1000;

interface Anchor {
  x: number;
  y: number;
  align: "start" | "end";
}

/** What the menu hands to its owner: props for the element the gestures open it from, and the "⋯" button. */
export interface MenuParts {
  surface: Pick<HTMLAttributes<HTMLElement>, "onContextMenu" | "onPointerDown" | "onPointerMove" | "onPointerUp" | "onPointerCancel" | "onClickCapture"> & {
    className: string;
  };
  button: ReactNode;
  open: boolean;
}

interface ActionMenuProps {
  entries: MenuEntries;
  /** Names the menu and its button ("More actions for X"). */
  label: string;
  /** A line over the entries: what the menu is about. */
  heading?: string;
  /** Classes of the "⋯" button: its place and size in the owner's layout. */
  buttonClassName?: string;
  children: (parts: MenuParts) => ReactNode;
}

/**
 * THE contextual menu of the app, for a track, an album, an artist or a
 * playlist: the "⋯" button, a right click and a long press (touch) on the
 * element that takes `surface` all open the same menu, so the entries are
 * built once by the owner (`entries`) and every way in shows them.
 *
 * It is a Radix menu (role menu, arrow keys, Esc, type-ahead, submenus with
 * the right arrow) whose anchor is a point: where the pointer was, or under
 * the button. Focus goes back to what had it — the button, or the element
 * that was focused when the menu opened by gesture. A long press neither
 * selects text nor stops the page from scrolling (a drift beyond a few
 * pixels, or the browser taking over to scroll, cancels it), and the click
 * that ends it is swallowed.
 */
export function ActionMenu({ entries, label, heading, buttonClassName, children }: ActionMenuProps) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<Anchor>({ x: 0, y: 0, align: "start" });
  const [mounted, setMounted] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const press = useRef<{ timer: number; x: number; y: number } | null>(null);
  const swallowClickUntil = useRef(0);

  const openAt = useCallback((next: Anchor, focusTarget: HTMLElement | null) => {
    returnFocus.current = focusTarget;
    setAnchor(next);
    setMounted(true);
    setOpen(true);
  }, []);

  const cancelPress = useCallback(() => {
    if (press.current) window.clearTimeout(press.current.timer);
    press.current = null;
  }, []);
  useEffect(() => cancelPress, [cancelPress]);

  const focused = () => (document.activeElement instanceof HTMLElement && document.activeElement !== document.body ? document.activeElement : null);

  const onContextMenu = (e: MouseEvent<HTMLElement>) => {
    if (e.defaultPrevented) return;
    e.preventDefault();
    // From the keyboard (the menu key, Shift+F10) there is no pointer: open under the element.
    const box = e.currentTarget.getBoundingClientRect();
    const fromKeyboard = e.clientX === 0 && e.clientY === 0;
    openAt({ x: fromKeyboard ? box.left + 16 : e.clientX, y: fromKeyboard ? box.top + box.height / 2 : e.clientY, align: "start" }, focused());
  };

  const onPointerDown = (e: PointerEvent<HTMLElement>) => {
    swallowClickUntil.current = 0;
    if (e.pointerType !== "touch" || e.button !== 0) return;
    const { clientX: x, clientY: y } = e;
    const target = focused();
    cancelPress();
    press.current = {
      x,
      y,
      timer: window.setTimeout(() => {
        press.current = null;
        swallowClickUntil.current = Date.now() + CLICK_AFTER_LONG_PRESS_MS;
        openAt({ x, y, align: "start" }, target);
      }, LONG_PRESS_MS),
    };
  };

  const onPointerMove = (e: PointerEvent<HTMLElement>) => {
    const started = press.current;
    if (started && Math.hypot(e.clientX - started.x, e.clientY - started.y) > LONG_PRESS_SLOP_PX) cancelPress();
  };

  const onClickCapture = (e: MouseEvent<HTMLElement>) => {
    if (Date.now() >= swallowClickUntil.current) return;
    swallowClickUntil.current = 0;
    e.preventDefault();
    e.stopPropagation();
  };

  const onButton = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    const box = e.currentTarget.getBoundingClientRect();
    openAt({ x: box.right, y: box.bottom, align: "end" }, e.currentTarget);
  };

  const visible = tidy(entries);
  const surface: MenuParts["surface"] = {
    // No text selection and no iOS callout on a touch screen: the press is the menu's.
    className: "[@media(hover:none)]:select-none [@media(hover:none)]:[-webkit-touch-callout:none]",
    onContextMenu,
    onPointerDown,
    onPointerMove,
    onPointerUp: cancelPress,
    onPointerCancel: cancelPress,
    onClickCapture,
  };
  const button = (
    <button
      ref={buttonRef}
      type="button"
      aria-label={label}
      title={label}
      aria-haspopup="menu"
      aria-expanded={open}
      onClick={onButton}
      className={cn(
        "grid size-11 shrink-0 place-items-center rounded-md text-muted-foreground transition-colors",
        "hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        buttonClassName,
      )}
    >
      <MoreHorizontal aria-hidden className="size-4" />
    </button>
  );

  return (
    <>
      {children({ surface, button, open })}
      <DropdownMenu open={open} onOpenChange={setOpen}>
        {/* The anchor: a point. In the body, since a row's `content-visibility` would make it the containing block of anything fixed. */}
        {mounted &&
          createPortal(
            <DropdownMenuTrigger asChild>
              <span aria-label={label} className="pointer-events-none fixed size-0" style={{ left: anchor.x, top: anchor.y }} />
            </DropdownMenuTrigger>,
            document.body,
          )}
        <DropdownMenuContent
          align={anchor.align}
          sideOffset={4}
          className="max-h-[min(24rem,var(--radix-dropdown-menu-content-available-height))] w-64 max-w-[calc(100vw-1rem)] overflow-y-auto"
          onCloseAutoFocus={(e) => {
            // Back to what had the focus, not to the point the menu hung from.
            e.preventDefault();
            const target = returnFocus.current;
            if (target?.isConnected) target.focus();
          }}
        >
          {heading && <DropdownMenuLabel className="truncate px-2 py-1.5 text-xs font-medium text-muted-foreground">{heading}</DropdownMenuLabel>}
          {visible.length === 0 && <p className="px-2 py-1.5 text-sm text-muted-foreground">{t("menu.empty")}</p>}
          {visible.map((entry, index) => (
            <Entry key={entry.type === "separator" ? `sep-${index}` : entry.id} entry={entry} />
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </>
  );
}

const ITEM = "min-h-11 sm:min-h-0";

function Entry({ entry }: { entry: MenuEntry }) {
  if (entry.type === "separator") return <DropdownMenuSeparator />;
  if (entry.type === "submenu") {
    return (
      <DropdownMenuSub>
        <DropdownMenuSubTrigger disabled={entry.disabled} className={ITEM}>
          {entry.icon}
          <span className="min-w-0 flex-1 truncate">{entry.label}</span>
        </DropdownMenuSubTrigger>
        <DropdownMenuSubContent className="max-h-72 w-60 max-w-[calc(100vw-1rem)] overflow-y-auto">{entry.content}</DropdownMenuSubContent>
      </DropdownMenuSub>
    );
  }
  return (
    <DropdownMenuItem
      disabled={entry.disabled}
      title={entry.hint}
      onSelect={entry.onSelect}
      className={cn(ITEM, entry.destructive && "text-destructive focus:text-destructive")}
    >
      {entry.icon}
      <span className="min-w-0 flex-1 truncate">{entry.label}</span>
    </DropdownMenuItem>
  );
}
