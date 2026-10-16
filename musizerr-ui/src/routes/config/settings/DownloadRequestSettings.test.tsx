import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider, LOCALE_STORAGE_KEY } from "@/i18n";
import { messages } from "@/i18n/messages";
import { SettingsArea } from "./SettingsArea";
import type { ConfigSchemaResponse, SettingDef } from "@/lib/api-client";
import type { RequestKind } from "@/types";

// The settings of the approval policy, as GET /settings/schema describes them
// (core/settings_catalog.py `_download_request_settings`, SettingDef.to_public):
// a mode and a threshold per kind of request, then the quota. The settings
// area renders them from the catalogue, knowing nothing about download requests.

const mockSchema = vi.fn();
const mockValues = vi.fn();
const mockPatch = vi.fn();
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  getSettingsSchema: () => mockSchema(),
  getSettingsValues: () => mockValues(),
  getProviders: async () => ({ metadata: [], download: [], lyrics: [], storage: [], sync: [], identification: [] }),
  patchSettings: (...a: unknown[]) => mockPatch(...a),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const KINDS: RequestKind[] = ["track", "album", "playlist", "artist", "liked_elsewhere"];

function setting(key: string, over: Partial<SettingDef> & Pick<SettingDef, "type">): SettingDef {
  return {
    key,
    group: "download_requests",
    area: "library",
    // The catalogue's own (Italian) text: the fallback when the dictionary has no entry.
    label: `Catalogo: ${key}`,
    description: `Descrizione: ${key}`,
    label_key: `settings.labels.${key}`,
    description_key: `settings.descriptions.${key}`,
    admin_only: true,
    scope: "system",
    options: null,
    minimum: null,
    maximum: null,
    step: null,
    secret: false,
    provider_category: null,
    required: true,
    hidden: false,
    visible_when: null,
    ...over,
  };
}

const modeKey = (kind: string) => `download_request_${kind}_mode`;
const thresholdKey = (kind: string) => `download_request_${kind}_threshold`;

const SETTINGS: SettingDef[] = [
  ...KINDS.flatMap((kind) => [
    setting(modeKey(kind), {
      type: "enum",
      options: ["auto", "approval", "auto_up_to"],
      option_label_keys: ["auto", "approval", "auto_up_to"].map((option) => `settings.options.${modeKey(kind)}.${option}`),
    }),
    setting(thresholdKey(kind), { type: "int", minimum: 1, maximum: 100000, step: 1, visible_when: [[modeKey(kind), "auto_up_to"]] }),
  ]),
  setting("download_request_quota_tracks", { type: "int", minimum: 0, maximum: 1000000, step: 10 }),
  setting("download_request_quota_days", { type: "int", minimum: 1, maximum: 365, step: 1 }),
];

const SCHEMA: ConfigSchemaResponse = {
  settings: SETTINGS,
  taxonomy: {
    areas: [{ key: "library", label_key: "settings.areas.library", label: "Libreria" }],
    groups: [
      {
        key: "download_requests",
        label_key: "settings.groups.download_requests",
        label: "Richieste di download",
        description_key: "settings.group_descriptions.download_requests",
        description: "…",
        area: "library",
      },
    ],
  },
};

/** The defaults of the backend. */
const VALUES = {
  download_request_track_mode: "auto",
  download_request_track_threshold: 1,
  download_request_album_mode: "auto_up_to",
  download_request_album_threshold: 30,
  download_request_playlist_mode: "approval",
  download_request_playlist_threshold: 30,
  download_request_artist_mode: "approval",
  download_request_artist_threshold: 30,
  download_request_liked_elsewhere_mode: "auto_up_to",
  download_request_liked_elsewhere_threshold: 50,
  download_request_quota_tracks: 200,
  download_request_quota_days: 7,
};

function renderArea() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
      <I18nProvider>
        <SettingsArea area="library" />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

const modeSelect = (label: string) => screen.getByRole("combobox", { name: label }) as HTMLSelectElement;
const thresholdInput = (label: string) => screen.queryByRole("spinbutton", { name: label }) as HTMLInputElement | null;

beforeEach(() => {
  mockSchema.mockReset().mockResolvedValue(SCHEMA);
  mockValues.mockReset().mockResolvedValue(VALUES);
  mockPatch.mockReset();
});

describe("the download request policy in the generated settings area", () => {
  it("one named group, with its description, holding every rule", async () => {
    renderArea();

    expect(await screen.findByRole("heading", { name: "Download requests" })).toBeInTheDocument();
    expect(screen.getByText(/What happens when a user asks for a download, for each kind of request: it starts at once \(Automatic\)/)).toBeInTheDocument();
    for (const label of ["Track requests", "Album requests", "Playlist requests", "Whole-artist requests", "Likes download requests"]) {
      expect(modeSelect(label)).toBeInTheDocument();
    }
    expect(screen.getByRole("spinbutton", { name: "Quota per user (tracks)" })).toHaveValue(200);
    expect(screen.getByRole("spinbutton", { name: "Quota period (days)" })).toHaveValue(7);
  });

  it("each mode is a select: automatic, needs approval, automatic up to a limit", async () => {
    renderArea();
    await screen.findByRole("heading", { name: "Download requests" });

    const select = modeSelect("Playlist requests");
    expect(select.value).toBe("approval");
    expect(within(select).getAllByRole("option").map((o) => [o.getAttribute("value"), o.textContent])).toEqual([
      ["auto", "Automatic"],
      ["approval", "Needs approval"],
      ["auto_up_to", "Automatic up to a limit"],
    ]);
    // The setting says what it is about; what the modes do is said once, in the group's description.
    expect(screen.getByText("What happens when a user asks for a playlist.")).toBeInTheDocument();
  });

  it("the threshold is there only for 'automatic up to a limit'", async () => {
    renderArea();
    await screen.findByRole("heading", { name: "Download requests" });

    expect(thresholdInput("Largest album request that starts at once (tracks)")).toHaveValue(30);
    expect(thresholdInput("Largest likes download that starts at once (tracks)")).toHaveValue(50);
    expect(thresholdInput("Largest playlist request that starts at once (tracks)")).toBeNull();
    expect(thresholdInput("Largest track request that starts at once (tracks)")).toBeNull();
  });

  it("choosing the mode shows its threshold at once, before saving, and choosing another hides it", async () => {
    renderArea();
    await screen.findByRole("heading", { name: "Download requests" });

    fireEvent.change(modeSelect("Playlist requests"), { target: { value: "auto_up_to" } });
    expect(thresholdInput("Largest playlist request that starts at once (tracks)")).toHaveValue(30);
    fireEvent.change(modeSelect("Album requests"), { target: { value: "auto" } });
    expect(thresholdInput("Largest album request that starts at once (tracks)")).toBeNull();
  });

  it("a threshold sits under its mode, indented; the modes and the quota do not", async () => {
    renderArea();
    await screen.findByRole("heading", { name: "Download requests" });

    const indented = (el: HTMLElement) => el.closest("div.border-l-2") !== null;
    expect(indented(thresholdInput("Largest album request that starts at once (tracks)")!)).toBe(true);
    expect(indented(modeSelect("Album requests"))).toBe(false);
    expect(indented(screen.getByRole("spinbutton", { name: "Quota per user (tracks)" }))).toBe(false);
  });

  it("saves the changes through the settings, with the keys of the catalogue", async () => {
    mockPatch.mockResolvedValue({ ...VALUES, download_request_playlist_mode: "auto_up_to", download_request_playlist_threshold: 40, download_request_quota_tracks: 0 });
    renderArea();
    await screen.findByRole("heading", { name: "Download requests" });

    fireEvent.change(modeSelect("Playlist requests"), { target: { value: "auto_up_to" } });
    fireEvent.change(thresholdInput("Largest playlist request that starts at once (tracks)")!, { target: { value: "40" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Quota per user (tracks)" }), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mockPatch).toHaveBeenCalledWith({
        download_request_playlist_mode: "auto_up_to",
        download_request_playlist_threshold: 40,
        download_request_quota_tracks: 0,
      }),
    );
  });

  it("in Italian: the group, the labels and the options", async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "it");
    renderArea();

    expect(await screen.findByRole("heading", { name: "Richieste di download" })).toBeInTheDocument();
    const select = modeSelect("Richieste di una playlist");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["Automatica", "Serve approvazione", "Automatica fino a un limite"]);
    expect(thresholdInput("Album più grande che parte subito (brani)")).toHaveValue(30);
    expect(screen.getByRole("spinbutton", { name: "Quota per utente (brani)" })).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "Periodo della quota (giorni)" })).toBeInTheDocument();
  });
});

describe("the dictionaries have every key the catalogue points to", () => {
  const path = (root: unknown, ...keys: string[]) => keys.reduce<any>((node, key) => node?.[key], root);

  it.each(["en", "it"] as const)("%s: a label and a description for each setting, an option label for each mode, the group", (locale) => {
    const dictionary = messages[locale];
    for (const def of SETTINGS) {
      expect(path(dictionary, "settings", "labels", def.key), `${locale} label ${def.key}`).toBeTruthy();
      expect(path(dictionary, "settings", "descriptions", def.key), `${locale} description ${def.key}`).toBeTruthy();
      for (const option of def.options ?? []) {
        expect(path(dictionary, "settings", "options", def.key, option), `${locale} option ${def.key}.${option}`).toBeTruthy();
      }
    }
    expect(path(dictionary, "settings", "groups", "download_requests")).toBeTruthy();
    expect(path(dictionary, "settings", "group_descriptions", "download_requests")).toBeTruthy();
  });
});
