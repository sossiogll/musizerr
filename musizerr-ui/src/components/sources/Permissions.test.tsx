import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { I18nProvider } from "@/i18n";
import { emptyDiff, noPermissions, reachPermissions } from "@/test/sources";
import type { PermissionsDiff } from "@/types";
import { PermissionsDiffView } from "./PermissionsDiffView";
import { PermissionsList } from "./PermissionsList";

const renderIn = (ui: React.ReactElement) => render(<I18nProvider>{ui}</I18nProvider>);

describe("PermissionsList", () => {
  it("says every host, setting-named address, folder and limit in words", () => {
    renderIn(<PermissionsList permissions={reachPermissions} />);

    const list = screen.getByRole("region", { name: "Permissions" });
    const lines = within(list).getAllByRole("listitem").map((li) => li.textContent);
    expect(lines).toEqual([
      "Can connect to api.example.org:443",
      "Can connect to the address you set in “server_url”",
      "Can open connections on any port to the host you set in “ftp_host”",
      "Can read and write the folder you set in “music_dir”",
      "Memory limit: 256 MB",
    ]);
  });

  it("a provider that declares nothing is said to reach nothing", () => {
    renderIn(<PermissionsList permissions={noPermissions} />);

    expect(screen.getByText("It cannot reach any network or folder and runs with the default limits.")).toBeInTheDocument();
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
  });

  it("a wildcard is any subdomain, never the domain itself", () => {
    renderIn(<PermissionsList permissions={{ ...noPermissions, egress: ["*.cdn.example.com:443", "cdn.example.com:443"] }} />);

    expect(screen.getByText("Can connect to any subdomain of cdn.example.com on port 443 (cdn.example.com itself is not included)")).toBeInTheDocument();
    expect(screen.getByText("Can connect to cdn.example.com:443")).toBeInTheDocument();
  });

  it("raw TCP says any port or the ports the manifest declares", () => {
    renderIn(<PermissionsList permissions={{ ...noPermissions, tcp_from_settings: [{ setting: "host", ports: "any" }, { setting: "data", ports: "2121" }] }} />);

    expect(screen.getByText("Can open connections on any port to the host you set in “host”")).toBeInTheDocument();
    expect(screen.getByText("Can open connections on ports 2121 to the host you set in “data”")).toBeInTheDocument();
  });

  it("a read-only folder is not said to be writable", () => {
    renderIn(<PermissionsList permissions={{ ...noPermissions, filesystem_from_settings: [{ setting: "cache", mode: "ro" }] }} />);

    expect(screen.getByText("Can read the folder you set in “cache”")).toBeInTheDocument();
  });
});

describe("PermissionsDiffView", () => {
  const diff: PermissionsDiff = {
    egress: { added: ["new.example:443"], removed: ["old.example:443"] },
    egress_from_settings: { added: ["server_url"], removed: [] },
    filesystem_from_settings: { added: [{ setting: "music_dir", mode: "rw" }], removed: [{ setting: "music_dir", mode: "ro" }] },
    tcp_from_settings: { added: [{ setting: "ftp_host", ports: "any" }, { setting: "data_host", ports: "30000-30100" }], removed: [{ setting: "old_host", ports: "21" }] },
    limits: [{ name: "memory_mb", old: null, new: 512 }],
    roles: { added: ["storage"], removed: [] },
    is_empty: false,
    grants_more: true,
  };

  it("lists what is added, what is removed, the limits that move and the roles gained", () => {
    renderIn(<PermissionsDiffView diff={diff} />);

    const added = screen.getByRole("region", { name: "Added" });
    expect(within(added).getByText("Can connect to new.example:443")).toBeInTheDocument();
    expect(within(added).getByText("Can connect to the address you set in “server_url”")).toBeInTheDocument();
    expect(within(added).getByText("Can read and write the folder you set in “music_dir”")).toBeInTheDocument();
    expect(within(added).getByText("Can open connections on any port to the host you set in “ftp_host”")).toBeInTheDocument();
    expect(within(added).getByText("Can open connections on ports 30000-30100 to the host you set in “data_host”")).toBeInTheDocument();
    expect(within(added).getByText("memory mb: default → 512")).toBeInTheDocument();
    const removed = screen.getByRole("region", { name: "Removed" });
    expect(within(removed).getByText("Can connect to old.example:443")).toBeInTheDocument();
    expect(within(removed).getByText("Can open connections on ports 21 to the host you set in “old_host”")).toBeInTheDocument();
    expect(within(removed).getByText("Can read the folder you set in “music_dir”")).toBeInTheDocument();
    expect(screen.getByText("It now also provides: storage")).toBeInTheDocument();
  });

  it("warns when the update asks for more than was approved", () => {
    renderIn(<PermissionsDiffView diff={diff} />);

    expect(screen.getByRole("alert")).toHaveTextContent("This update asks for more than you approved.");
  });

  it("only dropping permissions is no warning", () => {
    renderIn(<PermissionsDiffView diff={{ ...diff, egress: { added: [], removed: ["old.example:443"] }, egress_from_settings: { added: [], removed: [] }, filesystem_from_settings: { added: [], removed: [] }, tcp_from_settings: { added: [], removed: [] }, limits: [], roles: { added: [], removed: [] }, grants_more: false }} />);

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Added" })).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Removed" })).toBeInTheDocument();
  });

  it("an update that changes nothing says so", () => {
    renderIn(<PermissionsDiffView diff={emptyDiff} />);

    expect(screen.getByText("It asks for the same permissions you approved.")).toBeInTheDocument();
  });
});
