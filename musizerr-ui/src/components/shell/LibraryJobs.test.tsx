import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor } from "@testing-library/react";
import { viewerStatus } from "@/test/viewer";
import { renderPage } from "@/test/render";
import { queryKeys } from "@/lib/query-keys";
import { LibraryJobIndicator } from "./LibraryJobIndicator";
import { LibraryJobsWatcher } from "./LibraryJobsWatcher";
import type { AuthConfig, LibraryJobRun, LibraryJobs } from "@/types";

let status: AuthConfig = viewerStatus("user");
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status, user: { id: "u1" } }) }));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }));
vi.mock("sonner", () => ({ toast }));

let jobs: LibraryJobs;
vi.mock("@/lib/api-client", () => ({
  getLibraryJobs: async () => jobs,
  getPublicProviders: async () => ({ providers: [{ id: "prov", label: "Provider" }], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
}));

const run = (kind: LibraryJobRun["kind"], state: LibraryJobRun["status"], extra: Partial<LibraryJobRun> = {}): LibraryJobRun => ({
  kind, status: state, id: `${kind}-1`, processed: 0, total: null, providers: [], result: {}, ...extra,
});

const idle = (): LibraryJobs => ({
  library_sync: { kind: "library_sync", status: "never_run" },
  download_sync: { kind: "download_sync", status: "never_run" },
});

function Shell() {
  return (
    <>
      <LibraryJobIndicator withText />
      <LibraryJobsWatcher />
    </>
  );
}

const renderShell = () => renderPage(() => <Shell />, { path: "/", otherPaths: ["/favorites"] });

/** A new state from the server (a poll or a stream event). */
const serverSays = (client: ReturnType<typeof renderShell>["queryClient"], next: LibraryJobs) =>
  act(() => {
    client.setQueryData(queryKeys.libraryJobs, next);
  });

beforeEach(() => {
  status = viewerStatus("user");
  jobs = idle();
  Object.values(toast).forEach((fn) => fn.mockReset());
});

describe("the shell's library job indicator", () => {
  it("shows nothing while no job is active", async () => {
    const { queryClient } = renderShell();
    await waitFor(() => expect(queryClient.getQueryData(queryKeys.libraryJobs)).toBeDefined());
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows the running job's phase and a bar, and leads to the library", async () => {
    jobs.library_sync = run("library_sync", "running", { phase: "saving", provider: "prov", processed: 3, total: 4 });
    renderShell();
    const link = await screen.findByRole("link", { name: "Syncing likes: Saving likes from Provider: 3 of 4" });
    expect(link).toHaveAttribute("href", "/favorites");
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "75");
  });

  it("a queued job waits for the worker, with an indeterminate bar", async () => {
    jobs.download_sync = run("download_sync", "queued");
    renderShell();
    expect(await screen.findByRole("link", { name: "Downloading the ones liked elsewhere: Waiting for the worker" })).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
  });
});

describe("the end of a job, from any page", () => {
  it("a sync seen running then completed: a toast with the count", async () => {
    jobs.library_sync = run("library_sync", "running", { phase: "fetching", provider: "prov" });
    const { queryClient } = renderShell();
    await screen.findByRole("link");

    serverSays(queryClient, { ...idle(), library_sync: run("library_sync", "completed", { result: { tracks_synced: 7 } }) });

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Likes sync complete: 7 new"));
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("some providers failed: a warning, not a success", async () => {
    jobs.library_sync = run("library_sync", "running");
    const { queryClient } = renderShell();
    await screen.findByRole("link");

    serverSays(queryClient, {
      ...idle(),
      library_sync: run("library_sync", "completed", {
        providers: [
          { provider: "prov", status: "completed", processed: 1, total: 1, count: 1 },
          { provider: "other", status: "failed", processed: 0, total: null, count: 0, error_code: "provider_not_connected" },
        ],
      }),
    });

    await waitFor(() => expect(toast.warning).toHaveBeenCalledWith("Likes sync complete, but some services failed"));
  });

  it("a download sync that queued tracks says how many", async () => {
    jobs.download_sync = run("download_sync", "running", { phase: "checking", processed: 1, total: 3 });
    const { queryClient } = renderShell();
    await screen.findByRole("link");

    serverSays(queryClient, { ...idle(), download_sync: run("download_sync", "completed", { result: { total: 3, checked: 3, queued: 2, errors: 0 } }) });

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("2 tracks queued for download"));
  });

  it("a likes download that ended waiting for an admin says it was sent, never 'everything is in the library'", async () => {
    jobs.download_sync = run("download_sync", "running", { phase: "checking", processed: 1, total: 3 });
    const { queryClient } = renderShell();
    await screen.findByRole("link");

    serverSays(queryClient, {
      ...idle(),
      download_sync: run("download_sync", "completed", {
        result: {
          total: 120,
          checked: 120,
          queued: 0,
          errors: 0,
          skipped_removed: 0,
          pending_approval: { request_id: "req-likes", needed_tracks: 120, reason: "over_threshold" },
        },
      }),
    });

    await waitFor(() => expect(toast.info).toHaveBeenCalledWith("Sent for approval: 120 tracks to download", { description: "Larger than the automatic limit" }));
    expect(toast.info).not.toHaveBeenCalledWith("Everything liked elsewhere is already in the library");
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("worker_unavailable: a readable toast for a user, an actionable one for an admin", async () => {
    for (const [mode, text] of [
      ["user", "Background jobs can't run on the server right now. Try again later."],
      ["admin", "The worker isn't running: start the worker service, then try again."],
    ] as const) {
      status = viewerStatus(mode);
      toast.error.mockReset();
      jobs = { ...idle(), library_sync: run("library_sync", "queued") };
      const { queryClient, unmount } = renderShell();
      await screen.findByRole("link");

      serverSays(queryClient, { ...idle(), library_sync: run("library_sync", "failed", { error_code: "worker_unavailable", error_message: "No worker." }) });

      await waitFor(() => expect(toast.error).toHaveBeenCalledWith(text));
      unmount();
    }
  });

  it("a run already finished when the app opens is not announced", async () => {
    jobs.library_sync = run("library_sync", "completed", { result: { tracks_synced: 1 } });
    const { queryClient } = renderShell();
    await waitFor(() => expect(queryClient.getQueryData(queryKeys.libraryJobs)).toBeDefined());

    expect(toast.success).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });
});
