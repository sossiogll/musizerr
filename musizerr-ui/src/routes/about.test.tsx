import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { ATTRIBUTION, SOURCE_URL } from "@/lib/project";
import type { UpdatesInfo } from "@/lib/api-client";
import { renderPage } from "@/test/render";
import { viewerStatus } from "@/test/viewer";
import type { ViewerMode } from "@/types";
import { AboutPage } from "./about";

let mode: ViewerMode = "guest";
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: viewerStatus(mode), user: mode === "guest" ? null : { id: "u1" } }) }));

let updates: UpdatesInfo;
const getUpdates = vi.fn(async () => updates);
vi.mock("@/lib/api-client", () => ({ getUpdates: () => getUpdates() }));

const renderAbout = () => renderPage(AboutPage, { path: "/about" });

beforeEach(() => {
  mode = "guest";
  getUpdates.mockClear();
  updates = {
    current: "dev", commit: "", development: true, check_enabled: true, checked_at: null,
    last_check_failed: false, updates: [], latest: null,
  };
});

describe("AboutPage", () => {
  it("shows the attribution notice of NOTICE.md word for word, with the link", async () => {
    renderAbout();
    expect(ATTRIBUTION).toBe("Based on Musizerr — https://github.com/sossiogll/musizerr");
    const notice = await screen.findByText((_, element) => element?.tagName === "BLOCKQUOTE" && element.textContent === ATTRIBUTION);
    expect(notice).toBeVisible();
    expect(screen.getAllByRole("link", { name: "https://github.com/sossiogll/musizerr" })[0]).toHaveAttribute(
      "href",
      "https://github.com/sossiogll/musizerr",
    );
  });

  it("offers the source code, the license and the absence of warranty", async () => {
    renderAbout();
    expect(await screen.findByRole("heading", { name: "Source code" })).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: SOURCE_URL }).length).toBeGreaterThan(0);
    expect(screen.getByText(/GNU Affero General Public License, version 3/)).toBeInTheDocument();
    expect(screen.getByText(/WITHOUT ANY WARRANTY/)).toBeInTheDocument();
    expect(screen.getByText(/Copyright \(C\) 2026/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "The license (LICENSE)" })).toHaveAttribute("href", expect.stringContaining("/LICENSE"));
    expect(screen.getByRole("link", { name: "The additional terms (NOTICE.md)" })).toHaveAttribute("href", expect.stringContaining("/NOTICE.md"));
  });

  it("everyone sees the version of the web app; only an admin the server's", async () => {
    renderAbout();
    const section = (await screen.findByRole("heading", { name: "Version" })).parentElement!;
    expect(within(section).getByText("Web app")).toBeInTheDocument();
    expect(within(section).getByText("dev")).toBeInTheDocument();
    expect(within(section).queryByText("Server")).not.toBeInTheDocument();
    expect(getUpdates).not.toHaveBeenCalled();
  });

  it("an admin sees the server's version and commit", async () => {
    mode = "admin";
    updates = { ...updates, current: "1.2.0", commit: "abc1234def5678", development: false };
    renderAbout();
    const section = (await screen.findByRole("heading", { name: "Version" })).parentElement!;
    expect(await within(section).findByText("Server")).toBeInTheDocument();
    expect(within(section).getByText(/1\.2\.0/)).toHaveTextContent("1.2.0 (abc1234)");
  });
});
