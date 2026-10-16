import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// jsdom non implementa matchMedia: ThemeContext lo usa per la preferenza di
// sistema quando theme === "system". Stub minimale, sufficiente per i test
// (nessun test qui simula un cambio di preferenza del SO a runtime).
if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

// jsdom non implementa ResizeObserver: MarqueeText (usato ovunque un titolo
// possa scorrere, incl. TrackCard) lo usa in un effect per misurare se il
// testo overflow-a. Senza questo stub l'effect lancia, il boundary di
// TanStack Router lo intercetta e la pagina di test renderizza vuota.
if (typeof window !== "undefined" && !window.ResizeObserver) {
  window.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// jsdom non implementa né i dialog modali né la Fullscreen API, quindi nessun
// elemento soddisfa mai `:modal` o `:fullscreen`; il suo motore di selettori
// (nwsapi) però impiega SECONDI a dirlo. Floating UI li chiede a ogni
// posizionamento di un menu Radix (controllo del top layer): aprirne uno
// costava 3–12 s. La risposta esatta è "no", subito.
if (typeof Element !== "undefined") {
  const matches = Element.prototype.matches;
  const NEVER_IN_JSDOM = new Set([":modal", ":fullscreen"]);
  Element.prototype.matches = function (this: Element, selector: string) {
    return NEVER_IN_JSDOM.has(selector) ? false : matches.call(this, selector);
  };
}

// Smonta l'albero React e pulisce il DOM dopo ogni test.
afterEach(() => {
  cleanup();
  try {
    localStorage.clear();
  } catch {
    /* jsdom senza localStorage: ignora */
  }
});
