import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { I18nProvider, LOCALE_STORAGE_KEY } from "@/i18n";
import { ErrorMessage } from "./ErrorMessage";

function show(props: Parameters<typeof ErrorMessage>[0]) {
  return render(
    <I18nProvider>
      <ErrorMessage {...props} />
    </I18nProvider>,
  );
}

describe("ErrorMessage", () => {
  it("translates <subject>_<kind> codes", () => {
    show({ code: "sync_provider_unreachable", message: "The sync provider could not be reached." });
    expect(screen.getByText("The music server could not be reached.")).toBeInTheDocument();
  });

  it("prefers the longest kind (upstream_error, not error)", () => {
    show({ code: "metadata_provider_upstream_error", message: "x" });
    expect(screen.getByText("The catalog provider answered with an error.")).toBeInTheDocument();
  });

  it("translates a whole code when the dictionary has it", () => {
    show({ code: "database_busy", message: "The server is busy" });
    expect(screen.getByText("The server is busy, try again in a moment.")).toBeInTheDocument();
  });

  it("falls back to the backend's message for an unknown code", () => {
    show({ code: "something_new_happened", message: "Backend text" });
    expect(screen.getByText("Backend text")).toBeInTheDocument();
  });

  it("is in the viewer's language", () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "it");
    show({ code: "download_provider_timeout", message: "x" });
    expect(screen.getByText("Il provider di download ha impiegato troppo a rispondere.")).toBeInTheDocument();
  });

  it("shows the raw detail only when the server sent it (admins), collapsed", () => {
    const { rerender } = show({ code: "routine_failed", message: "x" });
    expect(screen.queryByText("Technical details")).not.toBeInTheDocument();

    rerender(
      <I18nProvider>
        <ErrorMessage code="routine_failed" message="x" detail="[Errno -3] Temporary failure" />
      </I18nProvider>,
    );
    const summary = screen.getByText("Technical details");
    expect(summary.closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText("[Errno -3] Temporary failure")).toBeInTheDocument();
  });
});
