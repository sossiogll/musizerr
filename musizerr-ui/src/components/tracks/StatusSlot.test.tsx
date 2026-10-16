import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/i18n";
import { DOWNLOAD_STATES } from "@/test/fixtures";
import { StatusSlot } from "./StatusSlot";

// The slot of an admin, who may download two ways: it carries the primary one
// only, as ONE plain button on every tier. The other ("and add to favorites")
// is an entry of the row's menu (components/menu/TrackMenu), not a second
// menu in the slot.

vi.mock("@/lib/api-client", () => ({ getDownloadStates: async () => DOWNLOAD_STATES }));

const onDownload = vi.fn();

function renderSlot(modes: ("plain" | "favorite")[] = ["plain", "favorite"]) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <I18nProvider>
        <StatusSlot state={{ kind: "available" }} title="Song A" modes={modes} onDownload={onDownload} />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

const primary = () => screen.getByRole("button", { name: "Download Song A" });

beforeEach(() => onDownload.mockReset());

describe("StatusSlot — the download button of an admin", () => {
  it("is one button, with no menu next to it", () => {
    renderSlot();
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: /Download options/ })).not.toBeInTheDocument();
    expect(primary()).not.toHaveAttribute("aria-haspopup");
  });

  it("is a 44px target on S/XS and the 32px-high bordered button from M up", () => {
    renderSlot();
    const classes = primary().className.split(/\s+/);
    expect(classes).toEqual(expect.arrayContaining(["h-11", "min-w-11", "rounded-md", "md:h-8", "md:border", "md:px-3"]));
    // No split: nothing to square off on its right.
    expect(classes).not.toContain("md:rounded-r-none");
  });

  it("downloads without the favorite, the primary way", () => {
    renderSlot();
    fireEvent.click(primary());
    expect(onDownload).toHaveBeenCalledTimes(1);
    expect(onDownload).toHaveBeenCalledWith("plain");
  });

  it("a user's one way is Download, which says it adds to favorites", () => {
    renderSlot(["favorite"]);
    fireEvent.click(screen.getByRole("button", { name: "Download Song A and add it to favorites" }));
    expect(onDownload).toHaveBeenCalledWith("favorite");
  });
});
