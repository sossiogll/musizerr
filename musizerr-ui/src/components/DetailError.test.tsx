import { describe, expect, it, vi } from "vitest";
import { AxiosError, AxiosHeaders } from "axios";
import { fireEvent, render, screen } from "@testing-library/react";
import { Disc3 } from "lucide-react";
import { I18nProvider } from "@/i18n";
import { DetailError } from "./DetailError";

const httpError = (status: number, data: unknown) =>
  new AxiosError("x", "ERR", undefined, undefined, { status, data, statusText: "", headers: {}, config: { headers: new AxiosHeaders() } });

const renderError = (error: unknown, onRetry = vi.fn()) =>
  render(
    <I18nProvider>
      <DetailError error={error} icon={Disc3} notFound="Album not found" onRetry={onRetry} />
    </I18nProvider>,
  );

describe("DetailError", () => {
  it("404: not found, nothing to retry", () => {
    renderError(httpError(404, { detail: "Album not found" }));
    expect(screen.getByText("Album not found")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("a provider that failed is not 'not found': it says so and offers Retry", () => {
    const onRetry = vi.fn();
    renderError(httpError(502, { detail: { error_code: "metadata_provider_upstream_error", error_message: "raw" } }), onRetry);
    expect(screen.queryByText("Album not found")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).not.toHaveTextContent("raw");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalled();
  });

  it("the API unreachable: a readable message and Retry", () => {
    renderError(new AxiosError("Network Error", "ERR_NETWORK"));
    expect(screen.getByRole("alert")).toHaveTextContent("This page could not be loaded.");
  });
});
