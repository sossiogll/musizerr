import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { I18nProvider } from "@/i18n";
import { ATTRIBUTION, SOURCE_URL } from "@/lib/project";
import { AboutPage } from "./about";

function renderAbout() {
  render(
    <I18nProvider>
      <AboutPage />
    </I18nProvider>,
  );
}

describe("AboutPage", () => {
  it("shows the attribution notice of NOTICE.md word for word, with the link", () => {
    renderAbout();
    expect(ATTRIBUTION).toBe("Based on Musizerr — https://github.com/sossiogll/musizerr");
    const notice = screen.getByText((_, element) => element?.tagName === "BLOCKQUOTE" && element.textContent === ATTRIBUTION);
    expect(notice).toBeVisible();
    expect(screen.getAllByRole("link", { name: "https://github.com/sossiogll/musizerr" })[0]).toHaveAttribute(
      "href",
      "https://github.com/sossiogll/musizerr",
    );
  });

  it("offers the source code, the license and the absence of warranty", () => {
    renderAbout();
    expect(screen.getByRole("heading", { name: "Source code" })).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: SOURCE_URL }).length).toBeGreaterThan(0);
    expect(screen.getByText(/GNU Affero General Public License, version 3/)).toBeInTheDocument();
    expect(screen.getByText(/WITHOUT ANY WARRANTY/)).toBeInTheDocument();
    expect(screen.getByText(/Copyright \(C\) 2026/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "The license (LICENSE)" })).toHaveAttribute("href", expect.stringContaining("/LICENSE"));
    expect(screen.getByRole("link", { name: "The additional terms (NOTICE.md)" })).toHaveAttribute("href", expect.stringContaining("/NOTICE.md"));
  });
});
