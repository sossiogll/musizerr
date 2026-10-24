import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { I18nProvider } from "@/i18n";
import { IncompleteResults, mergeProviderErrors } from "./IncompleteResults";

vi.mock("@/hooks/usePublicProviders", () => ({
  usePublicProviders: () => ({ label: (id: string) => ({ pa: "Provider A", pb: "Provider B" })[id] ?? id }),
}));

const err = (provider: string) => ({ provider, error_code: "metadata_provider_timeout", error_message: "x" });

describe("IncompleteResults", () => {
  it("names the providers that failed, by label", () => {
    render(
      <I18nProvider>
        <IncompleteResults errors={[err("pa"), err("pb")]} />
      </I18nProvider>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Results may be incomplete. Provider A and Provider B did not answer.");
  });

  it("inline, it is a quiet line for the one section it belongs to", () => {
    render(
      <I18nProvider>
        <IncompleteResults variant="inline" errors={[err("pa")]} />
      </I18nProvider>,
    );
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Results may be incomplete. Provider A did not answer.");
    expect(status).toHaveClass("text-xs", "text-muted-foreground");
    expect(status).not.toHaveClass("border");
  });

  it("renders nothing when every provider answered", () => {
    const { container } = render(
      <I18nProvider>
        <IncompleteResults errors={[]} />
      </I18nProvider>,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("merges several responses, one entry per provider", () => {
    expect(mergeProviderErrors([[err("pa")], undefined, [err("pa"), err("pb")]]).map((e) => e.provider)).toEqual(["pa", "pb"]);
  });
});
