import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { I18nProvider } from "@/i18n";
import { CoverageBar, CoverageShort } from "./LibraryCoverage";
import type { LibraryCoverage } from "@/types";

const coverage = (over: Partial<LibraryCoverage>): LibraryCoverage => ({ total: 10, downloaded: 1, in_progress: 0, failed: 0, ...over });

function renderWith(node: React.ReactNode) {
  render(<I18nProvider>{node}</I18nProvider>);
}

describe("library coverage", () => {
  it("a known total reads 'X/N'", () => {
    renderWith(<CoverageShort coverage={coverage({})} />);
    expect(screen.getByText("1/10 in library")).toBeInTheDocument();
  });

  it("an unknown total reads 'N in library', never 'N/N'", () => {
    renderWith(<CoverageShort coverage={coverage({ total: null })} />);
    expect(screen.getByText("1 in library")).toBeInTheDocument();
    expect(screen.queryByText(/1\/1/)).not.toBeInTheDocument();
  });

  it("the bar with an unknown total says how many are in the library", () => {
    renderWith(<CoverageBar coverage={coverage({ total: null, downloaded: 3 })} />);
    expect(screen.getByRole("img", { name: "3 in library" })).toBeInTheDocument();
  });
});
