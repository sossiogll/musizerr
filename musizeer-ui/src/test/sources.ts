import type {
  CatalogEntry,
  InstalledProvider,
  PermissionsDiff,
  ProviderGatewayState,
  ProviderPermissions,
  ProviderSource,
  SourceCatalogEntry,
} from "@/types";

/** Fixtures of Administration › Sources: no provider is named after a real one. */
export const NO_LIMITS = { memory_mb: null, open_files: null, processes: null };

export const reachPermissions: ProviderPermissions = {
  egress: ["api.example.org:443"],
  egress_from_settings: ["server_url"],
  filesystem_from_settings: [{ setting: "music_dir", mode: "rw" }],
  tcp_from_settings: [{ setting: "ftp_host", ports: "any" }],
  limits: { memory_mb: 256, open_files: null, processes: null },
};

export const noPermissions: ProviderPermissions = { egress: [], egress_from_settings: [], filesystem_from_settings: [], tcp_from_settings: [], limits: NO_LIMITS };

export const source = (overrides: Partial<ProviderSource> = {}): ProviderSource => ({
  id: "s1",
  name: "Team catalog",
  url: "https://catalog.example/registry.json",
  enabled: true,
  added_at: "2026-10-13T10:00:00+00:00",
  has_credential: false,
  allow_insecure_registry: false,
  ...overrides,
});

export const entry = (overrides: Partial<CatalogEntry> = {}): CatalogEntry => ({
  name: "demo",
  label: "Demo provider",
  description: "Does demo things",
  homepage: null,
  image: "ghcr.io/owner/musizeer-provider-demo",
  registry: "ghcr.io",
  digest: "sha256:" + "a".repeat(64),
  provider_api_version: 5,
  roles: ["lyrics"],
  permissions: reachPermissions,
  compatible: true,
  incompatibility: null,
  ...overrides,
});

export const catalogEntry = (overrides: Partial<SourceCatalogEntry> = {}): SourceCatalogEntry => ({
  ...entry(),
  installed: false,
  installed_from_this_source: false,
  installed_digest: null,
  update: null,
  ...overrides,
});

export const emptyDiff: PermissionsDiff = {
  egress: { added: [], removed: [] },
  egress_from_settings: { added: [], removed: [] },
  filesystem_from_settings: { added: [], removed: [] },
  tcp_from_settings: { added: [], removed: [] },
  limits: [],
  roles: { added: [], removed: [] },
  is_empty: true,
  grants_more: false,
};

export const installed = (overrides: Partial<InstalledProvider> = {}): InstalledProvider => ({
  name: "demo",
  label: "Demo provider",
  image: "ghcr.io/owner/musizeer-provider-demo",
  registry: "ghcr.io",
  digest: "sha256:" + "a".repeat(64),
  provider_api_version: 5,
  roles: ["lyrics"],
  permissions: reachPermissions,
  source_id: "s1",
  source_name: "Team catalog",
  sends_source_token: false,
  approved_at: "2026-10-13T10:00:00+00:00",
  approved_by: "admin-1",
  installed_at: "2026-10-13T10:00:00+00:00",
  update: null,
  update_error: null,
  ...overrides,
});

export const gateway = (overrides: Partial<ProviderGatewayState> = {}): ProviderGatewayState => ({
  reachable: true,
  status: "ok",
  detail: "",
  deployed: true,
  sandbox: { available: true, reason: "ok" },
  providers: [{ name: "demo", state: "running", reason: "", restarts: 0, roles: ["lyrics"] }],
  rejected: {},
  host_rejected: {},
  ...overrides,
});
