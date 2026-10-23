import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { viewerStatus } from "@/test/viewer";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { toast } from "sonner";
import { I18nProvider } from "@/i18n";
import { RoutinesPanel } from "./RoutinesPanel";
import type { RoutineInfo } from "@/lib/api-client";
import { LEGACY_ROUTINE, LIVE_ROUTINES, LIVE_SYNC_INDEX } from "@/test/live-routines";

const mockGetRoutines = vi.fn();
const mockPatchRoutine = vi.fn();
const mockRunRoutineNow = vi.fn();
const mockGetPublicProviders = vi.fn();
const mockUseAuth = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client");
  return {
    ...actual,
    getRoutines: (...args: unknown[]) => mockGetRoutines(...args),
    patchRoutine: (...args: unknown[]) => mockPatchRoutine(...args),
    runRoutineNow: (...args: unknown[]) => mockRunRoutineNow(...args),
    getPublicProviders: (...args: unknown[]) => mockGetPublicProviders(...args),
  };
});

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

function routine(overrides: Partial<RoutineInfo> & Pick<RoutineInfo, "key">): RoutineInfo {
  return {
    task: `workers.tasks.${overrides.key}`,
    enabled: true,
    interval_minutes: 60,
    min_interval_minutes: 5,
    last_run: null,
    next_run_at: null,
    queued: false,
    running: false,
    scheduler_stale: false,
    active_work: null,
    ...overrides,
  };
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <RoutinesPanel />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockGetRoutines.mockReset();
  mockPatchRoutine.mockReset();
  mockRunRoutineNow.mockReset();
  mockUseAuth.mockReset();
  mockUseAuth.mockReturnValue({ authConfig: viewerStatus("admin") });
  mockGetPublicProviders.mockReset();
  mockGetPublicProviders.mockResolvedValue({
    providers: [{ id: "musicserver", label: "Music Server" }],
    metadata: [],
    metadata_priority: [],
    metadata_capabilities: {},
  });
  vi.mocked(toast.success).mockReset();
  vi.mocked(toast.error).mockReset();
});

describe("RoutinesPanel", () => {
  it("renders nothing for a non-admin (no fetch either)", () => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user") });
    const { container } = renderPanel();
    expect(container).toBeEmptyDOMElement();
    expect(mockGetRoutines).not.toHaveBeenCalled();
  });

  it("renders each routine from the API response with its localized name and status", async () => {
    mockGetRoutines.mockResolvedValue([
      routine({
        key: "sync_saved_tracks",
        enabled: true,
        interval_minutes: 360,
        last_run: {
          queued_at: new Date().toISOString(),
          started_at: new Date().toISOString(),
          finished_at: new Date().toISOString(),
          wait_seconds: 0,
          duration_seconds: 0,
          status: "ok",
          result: null,
          detail: null,
          error_code: null,
        },
        next_run_at: new Date(Date.now() + 3600_000).toISOString(),
      }),
      routine({ key: "sweep_delegations", enabled: false, interval_minutes: 30 }),
    ]);

    renderPanel();

    await screen.findByText("Sync likes");
    expect(screen.getByText("Sweep delegations")).toBeInTheDocument();
    // Cadence hint rendered in hours for a 360-minute interval.
    expect(screen.getByText("every 6 h")).toBeInTheDocument();
    expect(screen.getByText("every 30 min")).toBeInTheDocument();
    // Last-run status badge for the routine that has run.
    expect(screen.getByText("OK")).toBeInTheDocument();
    // The other routine never ran.
    expect(screen.getByText("Never run yet")).toBeInTheDocument();
  });

  it("renders an unknown routine key without crashing, falling back to the raw key", async () => {
    mockGetRoutines.mockResolvedValue([routine({ key: "some_future_routine" })]);

    renderPanel();

    await screen.findByText("some_future_routine");
  });

  it("toggling the enable switch calls PATCH with the new value", async () => {
    mockGetRoutines.mockResolvedValue([routine({ key: "sweep_delegations", enabled: false })]);
    mockPatchRoutine.mockResolvedValue({ status: "ok" });

    renderPanel();

    await screen.findByText("Sweep delegations");
    fireEvent.click(screen.getByRole("switch"));

    await waitFor(() => expect(mockPatchRoutine).toHaveBeenCalledWith("sweep_delegations", { enabled: true }));
  });

  it("rejects a below-minimum interval client-side without calling PATCH", async () => {
    mockGetRoutines.mockResolvedValue([
      routine({ key: "sweep_delegations", interval_minutes: 30, min_interval_minutes: 5 }),
    ]);

    renderPanel();

    await screen.findByText("Sweep delegations");
    const input = screen.getByRole("spinbutton");
    fireEvent.change(input, { target: { value: "2" } });
    fireEvent.blur(input);

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Minimum is 5 min"));
    expect(mockPatchRoutine).not.toHaveBeenCalled();
    // Reverted to the last known-good value.
    expect(input).toHaveValue(30);
  });

  it("commits a valid interval change on blur", async () => {
    mockGetRoutines.mockResolvedValue([
      routine({ key: "sweep_delegations", interval_minutes: 30, min_interval_minutes: 5 }),
    ]);
    mockPatchRoutine.mockResolvedValue({ status: "ok" });

    renderPanel();

    await screen.findByText("Sweep delegations");
    const input = screen.getByRole("spinbutton");
    fireEvent.change(input, { target: { value: "45" } });
    fireEvent.blur(input);

    await waitFor(() =>
      expect(mockPatchRoutine).toHaveBeenCalledWith("sweep_delegations", { interval_minutes: 45 }),
    );
  });

  it("shows a toast and keeps the run button usable when 'Run now' hits a 409 (already running)", async () => {
    mockGetRoutines.mockResolvedValue([routine({ key: "sweep_delegations", running: false })]);
    mockRunRoutineNow.mockRejectedValue({ response: { status: 409 } });

    renderPanel();

    await screen.findByText("Sweep delegations");
    fireEvent.click(screen.getByRole("button", { name: "Run now" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Already queued or running"));
  });

  it("disables 'Run now' while the routine is already running", async () => {
    mockGetRoutines.mockResolvedValue([routine({ key: "sweep_delegations", running: true })]);

    renderPanel();

    await screen.findByText("Sweep delegations");
    expect(screen.getByRole("button", { name: "Running" })).toBeDisabled();
  });

  it("shows a finished one-time job as done with its last result, not as 'next run', and still lets it run", async () => {
    const finished = "2026-10-03T10:00:00+00:00";
    mockGetRoutines.mockResolvedValue([
      routine({
        key: "import_existing_library",
        done: true,
        next_run_at: null,
        last_run: {
          queued_at: finished, started_at: finished, finished_at: finished, wait_seconds: 0, duration_seconds: 42,
          status: "ok", result: { status: "ok", state: "imported", indexed: 812 }, detail: null, error_code: null, progress: null,
        },
      }),
    ]);

    renderPanel();

    await screen.findByText("Import the existing library");
    expect(screen.getByText("Done: it does not run again by itself")).toBeInTheDocument();
    expect(screen.getByText("812 audio files added to the library")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run now" })).toBeEnabled();
  });
});

// Real answers of GET /system/routines (src/test/live-routines.ts), in
// Europe/Rome (vitest.config.ts sets TZ): "now" is 12:35:57 UTC, 14:35:57
// in Rome. A time read without its offset would be two hours off.
describe("RoutinesPanel — real answers, Europe/Rome", () => {
  const NOW = new Date("2026-09-29T12:35:57.913Z");
  const live = (key: string, overrides: Partial<RoutineInfo> = {}): RoutineInfo => ({
    ...LIVE_ROUTINES.routines.find((r) => r.key === key)!,
    scheduler_stale: false,
    ...overrides,
  });

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
  });
  afterEach(() => vi.useRealTimers());

  it("runs in the zone the bug showed in", () => {
    expect(new Date(2026, 8, 29, 14, 35, 57, 913).toISOString()).toBe(NOW.toISOString());
  });

  it("says a run 3 minutes ago happened 3 minutes ago, with its result in words and its duration", async () => {
    mockGetRoutines.mockResolvedValue([live("match_likes")]);

    renderPanel();

    await screen.findByText("Match likes with the library");
    expect(screen.getByText("3 minutes ago")).toBeInTheDocument();
    expect(screen.getByText("33 of 1652 likes matched a track in the library")).toBeInTheDocument();
    expect(screen.getByText("took 1.1 s")).toBeInTheDocument();
    // Never the Python repr of the result.
    expect(screen.queryByText(/\{'/)).not.toBeInTheDocument();
  });

  it("reads a time without an offset as UTC, not as local time", async () => {
    mockGetRoutines.mockResolvedValue([{ ...LEGACY_ROUTINE, scheduler_stale: false }]);

    renderPanel();

    await screen.findByText("Like reconciliation");
    expect(screen.getByText("2 minutes ago")).toBeInTheDocument();
    expect(screen.queryByText(/hours ago/)).not.toBeInTheDocument();
  });

  it("the next run is in the future by the same clock", async () => {
    mockGetRoutines.mockResolvedValue([live("reconcile_pending_likes")]);

    renderPanel();

    await screen.findByText("Like reconciliation");
    // Started 12:33:57 UTC, every 60 minutes: next at 13:33:57, in 58 minutes.
    expect(screen.getByText("in 58 minutes")).toBeInTheDocument();
    expect(screen.getByText("Went through 2 pending likes")).toBeInTheDocument();
  });

  it("shows a running routine as running, for how long, and the work it set going", async () => {
    const base = live("sync_saved_tracks");
    mockGetRoutines.mockResolvedValue([
      {
        ...base,
        running: true,
        active_work: 2,
        last_run: {
          ...base.last_run!,
          started_at: "2026-09-29T12:33:57.913106+00:00",
          finished_at: null,
          duration_seconds: null,
          status: "running",
          result: null,
        },
      },
    ]);

    renderPanel();

    await screen.findByText("Sync likes");
    expect(screen.getByText("Running for 2 min")).toBeInTheDocument();
    expect(screen.getByText("2 likes syncs queued or running")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Running" })).toBeDisabled();
  });

  it("shows a run waiting for a worker as queued since its dispatch, not as running", async () => {
    const base = live("match_likes");
    const queuedAt = new Date(Date.now() - 47 * 60_000).toISOString();
    mockGetRoutines.mockResolvedValue([
      {
        ...base,
        queued: true,
        running: false,
        last_run: {
          ...base.last_run!,
          queued_at: queuedAt,
          started_at: null,
          finished_at: null,
          wait_seconds: null,
          duration_seconds: null,
          status: "queued",
          result: null,
        },
      },
    ]);

    renderPanel();

    await screen.findByText("Match likes with the library");
    expect(screen.getByText(/^Queued for 47 min/)).toBeInTheDocument();
    expect(screen.queryByText(/Running for/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Queued" })).toBeDisabled();
  });

  it("shows how long a finished run executed and, apart, how long it waited", async () => {
    const base = live("match_likes");
    mockGetRoutines.mockResolvedValue([
      {
        ...base,
        last_run: {
          ...base.last_run!,
          queued_at: "2026-09-29T12:20:00+00:00",
          started_at: "2026-09-29T12:32:00+00:00",
          wait_seconds: 720,
          duration_seconds: 2,
        },
      },
    ]);

    renderPanel();

    await screen.findByText("Match likes with the library");
    expect(screen.getByText("took 2 s")).toBeInTheDocument();
    expect(screen.getByText("after 12 min in the queue")).toBeInTheDocument();
  });

  it("a result its routine has no sentence for is shown as labelled numbers", async () => {
    mockGetRoutines.mockResolvedValue([
      live("sweep_workspaces", { key: "future_routine", last_run: { ...live("sweep_workspaces").last_run!, result: { status: "ok", removed: 4, checked: 9 } } }),
    ]);

    renderPanel();

    await screen.findByText("future_routine");
    expect(screen.getByText("removed 4 · checked 9")).toBeInTheDocument();
  });

  it("a source that could not be read is an ok run that says so and why, not an error", async () => {
    const base = live("sweep_workspaces");
    mockGetRoutines.mockResolvedValue([
      live("sweep_workspaces", {
        key: "check_for_updates",
        last_run: { ...base.last_run!, status: "ok", result: { status: "ok", state: "unavailable", reason: "not_found", releases: [] } },
      }),
    ]);

    renderPanel();

    expect(await screen.findByText(/Release information unavailable \(the releases of the project are not public/)).toBeInTheDocument();
  });
});

describe("RoutinesPanel — the library index and the matching after it (real answers)", () => {
  const NOW = new Date("2026-10-03T18:45:00Z");

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
  });
  afterEach(() => vi.useRealTimers());

  it("shows the index result per provider, and that the matching follows it", async () => {
    mockGetRoutines.mockResolvedValue(LIVE_SYNC_INDEX.ok);

    renderPanel();

    await screen.findByText("Update the library from the music server");
    expect(
      await screen.findByText(
        ": 40 files listed · 40 new · 0 attached · 0 linked by id · 0 old ids removed · 0 no longer on the server",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Music Server")).toBeInTheDocument();
    expect(screen.getAllByText("OK")).toHaveLength(2);
    expect(
      screen.getByText("Runs after “Update the library from the music server”, and on its own interval when that one is off."),
    ).toBeInTheDocument();
    expect(screen.getByText("0 of 0 likes matched a track in the library")).toBeInTheDocument();
    expect(screen.queryByText(/Matched against the library as it was/)).not.toBeInTheDocument();
  });

  it("an unreachable music server is a failed run with its error, and the matching says it used the library as it was", async () => {
    mockGetRoutines.mockResolvedValue(LIVE_SYNC_INDEX.error);

    renderPanel();

    await screen.findByText("Update the library from the music server");
    // Never "OK" with the failure hidden: the index run is an error, its provider says why.
    expect(screen.getByText("Error")).toBeInTheDocument();
    expect(await screen.findByText("Music Server")).toBeInTheDocument();
    expect(screen.getByText("The music server could not be reached.")).toBeInTheDocument();
    expect(screen.getByText("[Errno -3] Temporary failure in name resolution")).toBeInTheDocument();
    // The matching ran (OK), on what the database knew, and says so.
    expect(screen.getByText("OK")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Matched against the library as it was: its last update from the music server failed. The music server could not be reached.",
      ),
    ).toBeInTheDocument();
  });

  it("a partial listing is named by its own code", async () => {
    const [index] = LIVE_SYNC_INDEX.error;
    const run = index.last_run!;
    const report = {
      sync_provider: "musicserver", status: "partial_listing", listed: 2, gone: 8, pruned: false,
      error_code: "sync_provider_partial_listing",
      error_message: "The sync provider listed only part of its library: nothing was removed.",
    };
    mockGetRoutines.mockResolvedValue([
      { ...index, last_run: { ...run, error_code: report.error_code, error_message: report.error_message,
        error_detail: null, result: { ...run.result, providers: [report], error_code: report.error_code } } },
    ]);

    renderPanel();

    expect(
      await screen.findByText("The music server listed only part of its library: nothing was removed."),
    ).toBeInTheDocument();
  });

  it("a long index shows where it is while it runs", async () => {
    mockGetRoutines.mockResolvedValue(LIVE_SYNC_INDEX.running);

    renderPanel();

    await screen.findByText("Update the library from the music server");
    expect(await screen.findByText("Indexing Music Server: 3200 of 7675 files")).toBeInTheDocument();
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "42"); // 3200 of 7675
    expect(screen.getByRole("button", { name: "Running" })).toBeDisabled();
  });
});
