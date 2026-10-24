import { describe, it, expect, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { I18nProvider, useI18n } from "./index";
import { en, it as itDict, messages } from "./messages";

/** Appiattisce un oggetto annidato in un set di dot-path foglia. */
function leafPaths(obj: object, prefix = ""): string[] {
  return Object.entries(obj).flatMap(([k, v]) => {
    const key = prefix ? `${prefix}.${k}` : k;
    // A plural message ({ one, other }) is one leaf.
    return typeof v === "string" || (v && typeof v === "object" && "other" in v) ? [key] : leafPaths(v as object, key);
  });
}

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <I18nProvider>{children}</I18nProvider>
);

beforeEach(() => {
  localStorage.clear();
});

describe("dizionari", () => {
  it("EN e IT hanno esattamente le stesse chiavi", () => {
    expect(new Set(leafPaths(itDict))).toEqual(new Set(leafPaths(en)));
  });

  it("nessuna traduzione IT è vuota", () => {
    for (const path of leafPaths(itDict)) {
      const value = path.split(".").reduce<any>((n, p) => n[p], itDict);
      expect(value, path).toBeTruthy();
    }
  });

  it("espone entrambe le locale", () => {
    expect(Object.keys(messages).sort()).toEqual(["en", "it"]);
  });
});

describe("useI18n", () => {
  it("default in inglese quando non c'è preferenza salvata", () => {
    const { result } = renderHook(() => useI18n(), { wrapper });
    expect(result.current.t("nav.home")).toBe("Home");
    expect(result.current.t("nav.downloads")).toBe("Downloads");
  });

  it("cambia lingua a runtime e traduce in italiano", () => {
    const { result } = renderHook(() => useI18n(), { wrapper });
    act(() => result.current.setLocale("it"));
    expect(result.current.locale).toBe("it");
    expect(result.current.t("nav.downloads")).toBe("Download");
    expect(result.current.t("settings.title")).toBe("Impostazioni");
  });

  it("persiste la scelta in localStorage", () => {
    const { result } = renderHook(() => useI18n(), { wrapper });
    act(() => result.current.setLocale("it"));
    expect(localStorage.getItem("musizeer-lang")).toBe("it");
  });

  it("interpola le variabili {{...}}", () => {
    const { result } = renderHook(() => useI18n(), { wrapper });
    expect(result.current.t("home.noResults", { query: "korn" })).toContain("korn");
  });

  it("sceglie la forma plurale secondo le regole della lingua", () => {
    const { result } = renderHook(() => useI18n(), { wrapper });
    act(() => result.current.setLocale("it"));
    expect(result.current.t("collection.allInFavorites", { count: 1 })).toBe("Il brano in libreria è nei tuoi preferiti");
    expect(result.current.t("collection.allInFavorites", { count: 3 })).toBe("I 3 in libreria sono nei tuoi preferiti");
    expect(result.current.t("common.trackCount", { count: 0 })).toBe("0 brani");
    act(() => result.current.setLocale("en"));
    expect(result.current.t("common.trackCount", { count: 1 })).toBe("1 track");
    expect(result.current.t("common.trackCount", { count: 0 })).toBe("0 tracks");
    expect(result.current.t("duplicates.confirmResolveTitle", { count: 2 })).toBe("Delete 2 files?");
  });

  it("la forma plurale vale anche per le chiavi note solo a runtime (tx)", () => {
    const { result } = renderHook(() => useI18n(), { wrapper });
    expect(result.current.tx("routines.activeWorkGeneric", "fallback", { count: 1 })).toBe("1 task in progress");
  });

  it("ogni messaggio plurale ha le stesse forme in tutte le lingue", () => {
    const plurals = (dict: object) =>
      leafPaths(dict).filter((path) => typeof path.split(".").reduce<any>((n, p) => n[p], dict) === "object");
    expect(new Set(plurals(itDict))).toEqual(new Set(plurals(en)));
    for (const path of plurals(itDict)) {
      const value = path.split(".").reduce<any>((n, p) => n[p], itDict);
      expect(value.one && value.other, path).toBeTruthy();
    }
  });

  it("una chiave mancante ritorna la chiave stessa (dev-visible)", () => {
    const { result } = renderHook(() => useI18n(), { wrapper });
    // @ts-expect-error chiave inesistente di proposito
    expect(result.current.t("does.not.exist")).toBe("does.not.exist");
  });
});
