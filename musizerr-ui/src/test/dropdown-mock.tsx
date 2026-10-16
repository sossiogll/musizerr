import { cloneElement, isValidElement, useState, createContext, useContext, type ReactElement, type ReactNode } from "react";

/**
 * A stand-in for components/ui/dropdown-menu in page tests: Radix menus are
 * driven through pointer events and portals, and the menu itself is
 * third-party code, not what a page test checks. A test of a menu's own
 * contract (ARIA, keyboard) uses the real one, e.g. StatusSlot.test.tsx.
 * Same roles (menu, menuitem) and the same `onSelect` contract.
 *
 *   vi.mock("@/components/ui/dropdown-menu", async () => (await import("@/test/dropdown-mock")).dropdownModule());
 */
const Open = createContext<{ open: boolean; setOpen: (v: boolean) => void }>({ open: false, setOpen: () => {} });

function DropdownMenu({ children, open: controlled, onOpenChange }: { children: ReactNode; open?: boolean; onOpenChange?: (open: boolean) => void }) {
  const [own, setOwn] = useState(false);
  // Controlled like Radix when `open` is given (ActionMenu opens at a point, not from its trigger).
  const open = controlled ?? own;
  const setOpen = (value: boolean) => {
    setOwn(value);
    onOpenChange?.(value);
  };
  return <Open.Provider value={{ open, setOpen }}>{children}</Open.Provider>;
}

function DropdownMenuTrigger({ children }: { children: ReactNode; asChild?: boolean }) {
  const { open, setOpen } = useContext(Open);
  if (!isValidElement(children)) return null;
  const child = children as ReactElement<{ onClick?: () => void; "aria-expanded"?: boolean }>;
  return cloneElement(child, { onClick: () => setOpen(!open), "aria-expanded": open });
}

function DropdownMenuContent({ children }: { children: ReactNode }) {
  const { open } = useContext(Open);
  return open ? <div role="menu">{children}</div> : null;
}

function DropdownMenuItem({
  children,
  onSelect,
  onClick,
  disabled,
}: {
  children: ReactNode;
  onSelect?: (e: Event) => void;
  onClick?: () => void;
  disabled?: boolean;
  className?: string;
}) {
  const { setOpen } = useContext(Open);
  return (
    <button
      type="button"
      role="menuitem"
      disabled={disabled}
      onClick={() => {
        onSelect?.(new Event("select"));
        onClick?.();
        setOpen(false);
      }}
    >
      {children}
    </button>
  );
}

function DropdownMenuCheckboxItem({
  children,
  checked,
  onCheckedChange,
}: {
  children: ReactNode;
  checked?: boolean;
  onCheckedChange?: (checked: boolean) => void;
  className?: string;
}) {
  const { setOpen } = useContext(Open);
  return (
    <button
      type="button"
      role="menuitemcheckbox"
      aria-checked={!!checked}
      onClick={() => {
        onCheckedChange?.(!checked);
        setOpen(false);
      }}
    >
      {children}
    </button>
  );
}

// A submenu is always expanded here: its trigger is an item, its content a group.
function DropdownMenuSubTrigger({ children }: { children: ReactNode; className?: string }) {
  return (
    <button type="button" role="menuitem" aria-haspopup="menu">
      {children}
    </button>
  );
}

const Radio = createContext<{ value?: string; onValueChange?: (v: string) => void }>({});

function DropdownMenuRadioGroup({ children, value, onValueChange }: { children: ReactNode; value?: string; onValueChange?: (v: string) => void }) {
  return <Radio.Provider value={{ value, onValueChange }}>{children}</Radio.Provider>;
}

function DropdownMenuRadioItem({ children, value }: { children: ReactNode; value: string; className?: string }) {
  const { value: selected, onValueChange } = useContext(Radio);
  const { setOpen } = useContext(Open);
  return (
    <button
      type="button"
      role="menuitemradio"
      aria-checked={selected === value}
      onClick={() => {
        onValueChange?.(value);
        setOpen(false);
      }}
    >
      {children}
    </button>
  );
}

const Passthrough = ({ children }: { children?: ReactNode }) => <>{children}</>;

export function dropdownModule() {
  return {
    DropdownMenu,
    DropdownMenuTrigger,
    DropdownMenuContent,
    DropdownMenuItem,
    DropdownMenuSeparator: () => <hr />,
    DropdownMenuLabel: Passthrough,
    DropdownMenuGroup: Passthrough,
    DropdownMenuSub: Passthrough,
    DropdownMenuSubTrigger,
    DropdownMenuSubContent: ({ children }: { children?: ReactNode }) => <div role="menu">{children}</div>,
    DropdownMenuCheckboxItem,
    DropdownMenuRadioGroup,
    DropdownMenuRadioItem,
  };
}
