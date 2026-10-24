import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { AxiosError, type AxiosResponse } from "axios";
import { connectivityMock } from "@/test/connectivity-mock";
import { renderPage } from "@/test/render";
import { catalogEntry, emptyDiff, entry, gateway, installed, noPermissions, reachPermissions, source } from "@/test/sources";
import type { InstalledProvider, ProviderGatewayState, ProviderSource, SourceCatalog } from "@/types";
import { AdminSourcesPage } from "./sources";

vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());

let sources: ProviderSource[];
let installedList: InstalledProvider[];
let catalog: SourceCatalog;
let gatewayState: ProviderGatewayState;
const mockAdd = vi.fn();
const mockEdit = vi.fn();
const mockRemove = vi.fn();
const mockInstall = vi.fn();
const mockUpdate = vi.fn();
const mockUninstall = vi.fn();
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  getSources: async () => sources,
  getInstalledProviders: async () => installedList,
  getSourceCatalog: async () => catalog,
  getProviderGatewayState: async () => gatewayState,
  addSource: (...a: unknown[]) => mockAdd(...a),
  editSource: (...a: unknown[]) => mockEdit(...a),
  removeSource: (...a: unknown[]) => mockRemove(...a),
  installProvider: (...a: unknown[]) => mockInstall(...a),
  updateProvider: (...a: unknown[]) => mockUpdate(...a),
  uninstallProvider: (...a: unknown[]) => mockUninstall(...a),
}));

const renderSources = () => renderPage(AdminSourcesPage, { path: "/admin/sources" });

function refused(code: string, message: string): AxiosError {
  const response = { status: 409, statusText: "", headers: {}, config: {} as never, data: { detail: { error_code: code, error_message: message } } } as AxiosResponse;
  return new AxiosError("refused", "ERR_BAD_REQUEST", undefined, undefined, response);
}

beforeEach(() => {
  connectivityMock.reset();
  sources = [source()];
  installedList = [];
  catalog = { source: source(), name: "Team catalog", entries: [catalogEntry()], rejected: [] };
  gatewayState = gateway({ providers: [] });
  mockAdd.mockReset().mockResolvedValue(source());
  mockEdit.mockReset().mockResolvedValue(source());
  mockRemove.mockReset().mockResolvedValue({ ok: true, uninstalled: [] });
  mockInstall.mockReset().mockResolvedValue(installed());
  mockUpdate.mockReset().mockResolvedValue(installed());
  mockUninstall.mockReset().mockResolvedValue({ ok: true });
});

describe("Sources list", () => {
  it("shows each source with its address and what is special about it", async () => {
    sources = [source(), source({ id: "s2", name: "Private", url: "https://private.example/c.json", has_credential: true, enabled: false }), source({ id: "s3", name: "Local", url: "http://dev/c.json", allow_insecure_registry: true })];
    renderSources();

    const list = await screen.findByRole("region", { name: "Sources" });
    const rows = await within(list).findAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(within(rows[0]).getByText("Team catalog")).toBeInTheDocument();
    expect(within(rows[0]).getByText("https://catalog.example/registry.json")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Token set")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Disabled")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Insecure registry")).toBeInTheDocument();
    expect(within(rows[0]).queryByText("Token set")).not.toBeInTheDocument();
  });

  it("says when there is no source", async () => {
    sources = [];
    renderSources();

    expect(await screen.findByText(/No sources yet/)).toBeInTheDocument();
  });

  it("a source is enabled or disabled with its switch", async () => {
    renderSources();

    fireEvent.click(await screen.findByRole("switch", { name: "Use Team catalog" }));

    await waitFor(() => expect(mockEdit).toHaveBeenCalledWith("s1", { enabled: false }));
  });

  it("removing a source asks first and names the providers that go with it", async () => {
    installedList = [installed({ label: "Demo provider" })];
    renderSources();

    fireEvent.click(await screen.findByRole("button", { name: "Remove Team catalog" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("Remove Team catalog?")).toBeInTheDocument();
    expect(within(dialog).getByText(/Demo provider/)).toBeInTheDocument();
    expect(mockRemove).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(mockRemove).toHaveBeenCalledWith("s1"));
  });

  it("removing a source nothing was installed from says so", async () => {
    renderSources();

    fireEvent.click(await screen.findByRole("button", { name: "Remove Team catalog" }));

    expect(await screen.findByText("The source is removed. No provider is installed from it.")).toBeInTheDocument();
  });
});

describe("Add and edit a source", () => {
  it("adds a source with a token typed in a password field", async () => {
    renderSources();
    fireEvent.click(await screen.findByRole("button", { name: "Add source" }));
    const dialog = await screen.findByRole("alertdialog");

    fireEvent.change(within(dialog).getByLabelText("Catalog address"), { target: { value: "https://private.example/c.json" } });
    const token = within(dialog).getByLabelText("Token (optional)");
    expect(token).toHaveAttribute("type", "password");
    fireEvent.change(token, { target: { value: "ghp_secret" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Add source" }));

    await waitFor(() => expect(mockAdd).toHaveBeenCalledWith({ url: "https://private.example/c.json", credential: "ghp_secret", allow_insecure_registry: false }));
  });

  it("a token needs an https address, and the insecure registry and a token exclude each other", async () => {
    renderSources();
    fireEvent.click(await screen.findByRole("button", { name: "Add source" }));
    const dialog = await screen.findByRole("alertdialog");
    const add = within(dialog).getByRole("button", { name: "Add source" });

    fireEvent.change(within(dialog).getByLabelText("Catalog address"), { target: { value: "http://plain.example/c.json" } });
    fireEvent.change(within(dialog).getByLabelText("Token (optional)"), { target: { value: "t" } });
    expect(within(dialog).getByText("A token is only sent to an https address.")).toBeInTheDocument();
    expect(add).toBeDisabled();

    fireEvent.change(within(dialog).getByLabelText("Token (optional)"), { target: { value: "" } });
    expect(add).toBeEnabled();
    const insecure = within(dialog).getByLabelText("Allow an insecure registry");
    fireEvent.click(insecure);
    expect(within(dialog).getByText(/plain HTTP, which anyone on the network can read/)).toBeInTheDocument();
    fireEvent.click(add);
    await waitFor(() => expect(mockAdd).toHaveBeenCalledWith({ url: "http://plain.example/c.json", allow_insecure_registry: true }));
  });

  it("the insecure warning is always on screen next to the checkbox", async () => {
    renderSources();
    fireEvent.click(await screen.findByRole("button", { name: "Add source" }));

    expect(await screen.findByText(/Use it only for a registry on your own machine/)).toBeInTheDocument();
  });

  it("a refusal is read in words inside the dialog, which stays open", async () => {
    mockAdd.mockRejectedValue(refused("source_exists", "that source is already added"));
    renderSources();
    fireEvent.click(await screen.findByRole("button", { name: "Add source" }));
    const dialog = await screen.findByRole("alertdialog");

    fireEvent.change(within(dialog).getByLabelText("Catalog address"), { target: { value: "https://catalog.example/registry.json" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Add source" }));

    expect(await within(dialog).findByText("That source is already added.")).toBeInTheDocument();
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  });

  it("editing a source never shows its token: only that there is one, and it can be replaced or removed", async () => {
    sources = [source({ has_credential: true })];
    renderSources();
    fireEvent.click(await screen.findByRole("button", { name: "Edit Team catalog" }));
    const dialog = await screen.findByRole("alertdialog");

    const token = within(dialog).getByLabelText("Token (optional)");
    expect(token).toHaveValue("");
    expect(token).toHaveAttribute("placeholder", "Leave empty to keep the current token");
    expect(within(dialog).getByLabelText("Catalog address")).toHaveAttribute("readonly");

    fireEvent.click(within(dialog).getByLabelText("Remove the current token"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mockEdit).toHaveBeenCalledWith("s1", { allow_insecure_registry: false, credential: null }));
  });

  it("saving without touching the token leaves it out of the request", async () => {
    sources = [source({ has_credential: true })];
    renderSources();
    fireEvent.click(await screen.findByRole("button", { name: "Edit Team catalog" }));
    const dialog = await screen.findByRole("alertdialog");

    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mockEdit).toHaveBeenCalledWith("s1", { allow_insecure_registry: false }));
  });
});

describe("Catalog browser and install", () => {
  it("lists the entries of an enabled source and not those of a disabled one", async () => {
    sources = [source(), source({ id: "s2", name: "Off", enabled: false })];
    renderSources();

    const catalogs = await screen.findByRole("region", { name: "Catalogs" });
    expect(await within(catalogs).findByRole("region", { name: "Catalog of Team catalog" })).toBeInTheDocument();
    expect(within(catalogs).queryByRole("region", { name: "Catalog of Off" })).not.toBeInTheDocument();
    expect(within(catalogs).getByText("Demo provider")).toBeInTheDocument();
    expect(within(catalogs).getByText("Does demo things")).toBeInTheDocument();
  });

  it("an entry built for another API version is listed with the reason and cannot be installed", async () => {
    catalog = { ...catalog, entries: [catalogEntry({ name: "old", label: "Old one", compatible: false, incompatibility: "x", provider_api_version: 3 })] };
    renderSources();

    expect(await screen.findByText(/it was built for provider API version 3/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Install Old one" })).not.toBeInTheDocument();
  });

  it("an installed entry has no install button", async () => {
    catalog = { ...catalog, entries: [catalogEntry({ installed: true, installed_from_this_source: true, installed_digest: "sha256:" + "a".repeat(64) })] };
    renderSources();

    const region = await screen.findByRole("region", { name: "Catalog of Team catalog" });
    expect(await within(region).findByText("Installed")).toBeInTheDocument();
    expect(within(region).queryByRole("button", { name: /^Install/ })).not.toBeInTheDocument();
  });

  it("entries that are not valid are counted and explained", async () => {
    catalog = { ...catalog, rejected: [{ name: "broken", reason: "'digest' must be 'sha256:'" }] };
    renderSources();

    expect(await screen.findByText("1 entry of this catalog is not valid and was left out")).toBeInTheDocument();
    expect(screen.getByText(/'digest' must be/)).toBeInTheDocument();
  });

  it("the install dialog shows the permissions before anything is installed, then installs the digest it showed", async () => {
    renderSources();
    fireEvent.click(await screen.findByRole("button", { name: "Install Demo provider" }));
    const dialog = await screen.findByRole("alertdialog");

    expect(within(dialog).getByText("Install Demo provider")).toBeInTheDocument();
    expect(within(dialog).getByText("Can connect to api.example.org:443")).toBeInTheDocument();
    expect(within(dialog).getByText("Can connect to the address you set in “server_url”")).toBeInTheDocument();
    expect(within(dialog).getByText("Can read and write the folder you set in “music_dir”")).toBeInTheDocument();
    expect(within(dialog).getByText("Memory limit: 256 MB")).toBeInTheDocument();
    expect(within(dialog).getByText("Lyrics")).toBeInTheDocument();
    expect(within(dialog).getByText(`ghcr.io/owner/musizeer-provider-demo@${"a".repeat(12)}`)).toBeInTheDocument();
    expect(within(dialog).queryByText(/token of this source/)).not.toBeInTheDocument();
    expect(mockInstall).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Install and allow" }));
    await waitFor(() => expect(mockInstall).toHaveBeenCalledWith("s1", "demo", "sha256:" + "a".repeat(64)));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });

  it("a provider that asks for nothing says it reaches nothing", async () => {
    catalog = { ...catalog, entries: [catalogEntry({ permissions: noPermissions })] };
    renderSources();
    fireEvent.click(await screen.findByRole("button", { name: "Install Demo provider" }));

    expect(await screen.findByText("It cannot reach any network or folder and runs with the default limits.")).toBeInTheDocument();
  });

  it("a source with a token says where the token will be sent", async () => {
    sources = [source({ has_credential: true })];
    catalog = { ...catalog, source: source({ has_credential: true }) };
    renderSources();
    fireEvent.click(await screen.findByRole("button", { name: "Install Demo provider" }));

    expect(await screen.findByText("The token of this source will be sent to ghcr.io to download the image.")).toBeInTheDocument();
  });

  it("a catalog that changed meanwhile is refused and the dialog stays for another look", async () => {
    mockInstall.mockRejectedValue(refused("catalog_changed", "the catalog now offers another digest"));
    renderSources();
    fireEvent.click(await screen.findByRole("button", { name: "Install Demo provider" }));
    const dialog = await screen.findByRole("alertdialog");

    fireEvent.click(within(dialog).getByRole("button", { name: "Install and allow" }));

    expect(await within(dialog).findByText(/The catalog changed while you were looking at it/)).toBeInTheDocument();
  });

  it("cancelling installs nothing", async () => {
    renderSources();
    fireEvent.click(await screen.findByRole("button", { name: "Install Demo provider" }));
    const dialog = await screen.findByRole("alertdialog");

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(mockInstall).not.toHaveBeenCalled();
  });

  it("nothing can be changed while the server is unreachable", async () => {
    connectivityMock.set({ online: false });
    renderSources();

    expect(await screen.findByRole("button", { name: "Add source" })).toBeDisabled();
    expect(await screen.findByRole("button", { name: "Install Demo provider" })).toBeDisabled();
  });
});

describe("Installed providers", () => {
  it("shows each provider with its image, its source and the gateway's state", async () => {
    installedList = [installed()];
    gatewayState = gateway();
    renderSources();

    const region = await screen.findByRole("region", { name: "Installed providers" });
    const row = (await within(region).findAllByRole("listitem"))[0];
    expect(within(row).getByText("Demo provider")).toBeInTheDocument();
    expect(within(row).getByText("Lyrics")).toBeInTheDocument();
    expect(await within(row).findByText("Running")).toBeInTheDocument();
    expect(within(row).getByText(/From Team catalog/)).toBeInTheDocument();
    expect(within(row).getByText(`ghcr.io/owner/musizeer-provider-demo@${"a".repeat(12)}`)).toBeInTheDocument();
  });

  it("a provider the gateway refused shows the state and the reason", async () => {
    installedList = [installed()];
    gatewayState = gateway({ status: "degraded", providers: [{ name: "demo", state: "refused", reason: "the image declares permissions that are not the ones approved", restarts: 0, roles: ["lyrics"] }] });
    renderSources();

    const region = await screen.findByRole("region", { name: "Installed providers" });
    expect(await within(region).findByText("Refused")).toBeInTheDocument();
    expect(within(region).getByText(/not the ones approved/)).toBeInTheDocument();
  });

  it("a provider the gateway does not know yet says so", async () => {
    installedList = [installed()];
    gatewayState = gateway({ providers: [] });
    renderSources();

    expect(await screen.findByText("Not known to the gateway yet")).toBeInTheDocument();
  });

  it("a gateway that does not answer, or cannot make a sandbox, is said above the list", async () => {
    installedList = [installed()];
    gatewayState = gateway({ reachable: false, status: "unreachable", providers: [] });
    const { unmount } = renderSources();
    expect(await screen.findByText("The provider gateway does not answer, so no installed provider is running.")).toBeInTheDocument();
    unmount();

    gatewayState = gateway({ status: "unavailable", sandbox: { available: false, reason: "no user namespaces" }, providers: [] });
    renderSources();
    expect(await screen.findByText("No sandbox can be made on this server, so no provider is running: no user namespaces")).toBeInTheDocument();
  });

  it("what the gateway refused to take and what Musizeer refused to register is said next to the provider", async () => {
    installedList = [installed()];
    gatewayState = gateway({ rejected: { demo: "a digest is required" }, host_rejected: { demo: "what it says of itself differs from its image" } });
    renderSources();

    expect(await screen.findByText("The gateway refused Demo provider: a digest is required")).toBeInTheDocument();
    expect(screen.getByText("Running but not used by Musizeer: what it says of itself differs from its image")).toBeInTheDocument();
  });

  it("uninstalling asks first", async () => {
    installedList = [installed()];
    renderSources();

    fireEvent.click(await screen.findByRole("button", { name: "Uninstall Demo provider" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("Uninstall Demo provider?")).toBeInTheDocument();
    expect(mockUninstall).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Uninstall" }));
    await waitFor(() => expect(mockUninstall).toHaveBeenCalledWith("demo"));
  });

  it("an update shows what it changes and is applied only once approved, with the digest that was shown", async () => {
    const newer = "sha256:" + "b".repeat(64);
    installedList = [
      installed({
        update: {
          ...entry({ digest: newer, permissions: { ...reachPermissions, egress: ["api.example.org:443", "extra.example:443"] } }),
          diff: { ...emptyDiff, is_empty: false, grants_more: true, egress: { added: ["extra.example:443"], removed: [] } },
        },
      }),
    ];
    renderSources();

    fireEvent.click(await screen.findByRole("button", { name: "Review the update of Demo provider" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("Update Demo provider")).toBeInTheDocument();
    expect(within(dialog).getByRole("alert")).toHaveTextContent("This update asks for more than you approved.");
    const added = within(dialog).getByRole("region", { name: "Added" });
    expect(within(added).getByText("Can connect to extra.example:443")).toBeInTheDocument();
    expect(within(dialog).getByText(`${"a".repeat(12)} → ${"b".repeat(12)}`)).toBeInTheDocument();
    expect(mockUpdate).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Approve and update" }));
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith("demo", newer));
  });

  it("an update built for another API version cannot be approved", async () => {
    installedList = [installed({ update: { ...entry({ digest: "sha256:" + "c".repeat(64), compatible: false, provider_api_version: 9 }), diff: emptyDiff } })];
    renderSources();

    fireEvent.click(await screen.findByRole("button", { name: "Review the update of Demo provider" }));
    const dialog = await screen.findByRole("alertdialog");

    expect(within(dialog).getByText(/built for provider API version 9/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Approve and update" })).toBeDisabled();
  });

  it("an update that could not be looked up is said, in words", async () => {
    installedList = [installed({ update_error: "catalog_unreachable" })];
    renderSources();

    expect(await screen.findByText("Could not look for an update: The catalog's server could not be reached.")).toBeInTheDocument();
  });

  it("a disabled source is no error: its providers just have no update", async () => {
    installedList = [installed({ update_error: "source_disabled" })];
    renderSources();

    await screen.findByText("Demo provider");
    expect(screen.queryByText(/Could not look for an update/)).not.toBeInTheDocument();
  });
});

describe("Nothing secret reaches the page", () => {
  it("the page holds no token anywhere in its text, only whether there is one", async () => {
    sources = [source({ has_credential: true })];
    installedList = [installed({ sends_source_token: true })];
    const { container } = renderSources();

    await screen.findByText("Token set");
    expect(container.textContent).not.toMatch(/ghp_|credential/i);
  });
});
