import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { AxiosError, type AxiosResponse } from "axios";
import type { ReactNode } from "react";
import { I18nProvider, LOCALE_STORAGE_KEY } from "@/i18n";
import { useErrorText, useFailureToast } from "./error-text";

const mockToast = vi.hoisted(() => ({ error: vi.fn() }));
vi.mock("sonner", () => ({ toast: mockToast }));

const wrapper = ({ children }: { children: ReactNode }) => <I18nProvider>{children}</I18nProvider>;

const refused = (status: number, detail: unknown) =>
  new AxiosError(String(status), "ERR_BAD_REQUEST", undefined, undefined, { status, data: { detail } } as AxiosResponse);

beforeEach(() => {
  localStorage.clear();
  mockToast.error.mockReset();
});

// The error codes the approval policy adds (app/openapi.yaml: CodedErrorResponse, a download attempt's `error_code`).
describe("the error codes of the approval policy are translated", () => {
  it.each([
    ["retry_not_approved", "Only a download that was requested and approved can be retried."],
    ["request_already_decided", "An administrator already decided this request."],
    ["queue_unavailable", "The queue is unavailable, so this could not start. A download is tried again automatically; otherwise try again in a moment."],
  ])("%s, in English", (code, text) => {
    const { result } = renderHook(() => useErrorText(), { wrapper });
    expect(result.current(code, "the server's English message")).toBe(text);
  });

  it.each([
    ["retry_not_approved", "Si può ritentare solo un download che è stato richiesto e approvato."],
    ["request_already_decided", "Un amministratore ha già deciso questa richiesta."],
    ["queue_unavailable", "La coda non è disponibile, quindi non è partito. Un download viene ritentato in automatico; altrimenti riprova tra un momento."],
  ])("%s, in Italian", (code, text) => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "it");
    const { result } = renderHook(() => useErrorText(), { wrapper });
    expect(result.current(code, "the server's English message")).toBe(text);
  });

  it("a code nobody translated falls back to the server's own message", () => {
    const { result } = renderHook(() => useErrorText(), { wrapper });
    expect(result.current("something_new", "The server's message")).toBe("The server's message");
  });
});

describe("useFailureToast", () => {
  it("says a refusal in the viewer's words when the server gave a code", () => {
    const { result } = renderHook(() => useFailureToast(), { wrapper });
    act(() => result.current(refused(403, { error_code: "retry_not_approved", error_message: "Only a download that was requested and approved can be retried." }), "Download failed"));
    expect(mockToast.error).toHaveBeenCalledWith("Only a download that was requested and approved can be retried.");
  });

  it("a conflict says an administrator already decided", () => {
    const { result } = renderHook(() => useFailureToast(), { wrapper });
    act(() => result.current(refused(409, { error_code: "request_already_decided", error_message: "The request was already approved." }), "Could not cancel"));
    expect(mockToast.error).toHaveBeenCalledWith("An administrator already decided this request.");
  });

  it("with no code, the server's message; with no answer at all, the fallback", () => {
    const { result } = renderHook(() => useFailureToast(), { wrapper });
    act(() => result.current(refused(409, { error_code: null, error_message: "Plain message" }), "fallback"));
    expect(mockToast.error).toHaveBeenLastCalledWith("Plain message");
    act(() => result.current(new Error("network down"), "Could not cancel the request"));
    expect(mockToast.error).toHaveBeenLastCalledWith("Could not cancel the request");
  });
});
