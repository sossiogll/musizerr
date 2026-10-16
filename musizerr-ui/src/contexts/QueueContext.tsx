import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/contexts/AuthContext";
import { useCapabilities } from "@/hooks/useCapabilities";
import { createProgressSource, refreshAccessToken } from "@/lib/api-client";
import type { DownloadStatus, DownloadStep, StreamEvent } from "@/types";

/** Quante voci tengono al massimo le mappe degli stati live: le più vecchie
    escono per prime (i loro stati sono già nella lista dei download). */
export const LIVE_STATES_LIMIT = 500;
/** Attesa prima di riaprire uno stream chiuso dal server, raddoppiata a ogni
    tentativo fallito fino al massimo. */
export const RECONNECT_BASE_MS = 2_000;
export const RECONNECT_MAX_MS = 60_000;

/** ``map`` con ``key`` aggiornato come voce più recente, senza superare
    ``LIVE_STATES_LIMIT`` voci (le più vecchie escono per prime). */
function remember<T>(map: Map<string, T>, key: string, value: T): void {
  map.delete(key);
  map.set(key, value);
  while (map.size > LIVE_STATES_LIMIT) map.delete(map.keys().next().value as string);
}

export interface LiveTrackState {
  /** This attempt's status. */
  status: DownloadStatus;
  /** The track's file state, decided by the server (what a row shows). */
  file_state?: DownloadStatus | null;
  /** The track's id in the library database, as the server resolved it. */
  track_id?: string;
  step?: DownloadStep;
  provider_attempting?: string;
  method?: "id" | "fuzzy";
  retry?: number;
}

/**
 * Gli stati live in uno store esterno a React: ogni evento aggiorna le mappe
 * e avvisa solo chi osserva la voce cambiata (useLiveState), non ogni riga
 * di ogni lista — con centinaia di righe a schermo e un evento al secondo la
 * differenza si vede.
 */
class LiveStore {
  byTrack = new Map<string, LiveTrackState>();
  byDownload = new Map<string, LiveTrackState>();
  activeCount = 0;
  version = 0;
  private listeners = new Set<() => void>();
  private eventListeners = new Set<(event: StreamEvent) => void>();

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** Every raw event of the stream, for progress channels that are not a track download. */
  onEvent = (listener: (event: StreamEvent) => void) => {
    this.eventListeners.add(listener);
    return () => {
      this.eventListeners.delete(listener);
    };
  };

  emit(event: StreamEvent) {
    for (const listener of this.eventListeners) listener(event);
  }

  notify() {
    this.version += 1;
    for (const listener of this.listeners) listener();
  }
}

interface QueueContextValue {
  store: LiveStore;
  /** Notifica il context che nuovi download sono stati avviati */
  addItems: (ids: string[]) => void;
}

const QueueContext = createContext<QueueContextValue>({
  store: new LiveStore(),
  addItems: () => {},
});

export function QueueProvider({ children }: { children: React.ReactNode }) {
  const [store] = useState(() => new LiveStore());
  const activeIdsRef = useRef(new Set<string>());
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const { mode, can } = useCapabilities();

  // /progress segue i download: solo chi può scaricare lo ascolta. Con il
  // login attivo richiede il cookie di stream, sincronizzato prima che la
  // sessione sia pronta; lo stream si riapre a ogni nuovo login (user.id).
  const canListen = can.download;
  const authEnabled = mode !== "open";

  useEffect(() => {
    if (!canListen) return;
    let source: EventSource | null = null;
    let reconnect: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    let disposed = false;

    const onMessage = (event: MessageEvent) => {
      const payload: StreamEvent = JSON.parse(event.data);
      // Not a download (e.g. a library job's progress): only for its listeners.
      if (payload.type !== undefined) {
        store.emit(payload);
        return;
      }
      const { download_id, status } = payload;

      // Contatore dei download attivi: gli stati canonici ancora in corso
      // (core/download_states), mai per una traccia già in libreria (un nuovo
      // tentativo forzato non la rende "in corso").
      const ids = activeIdsRef.current;
      if (
        payload.file_state !== "downloaded" &&
        (status === "pending" ||
          status === "downloading" ||
          status === "importing" ||
          status === "retrying" ||
          status === "dispatched")
      ) {
        ids.add(download_id);
      } else {
        ids.delete(download_id);
      }
      store.activeCount = ids.size;

      const liveState: LiveTrackState = {
        status,
        file_state: payload.file_state,
        track_id: payload.track_id,
        step: payload.step,
        provider_attempting: payload.provider_attempting,
        method: payload.method,
        retry: payload.retry,
      };

      // Per track e, stesso evento, per download_id: una riga che ha appena
      // avviato il download conosce solo il download_id restituito dalla
      // API, non il track_id canonico risolto lato server.
      if (payload.track_id) remember(store.byTrack, payload.track_id, liveState);
      remember(store.byDownload, download_id, liveState);
      store.notify();
      store.emit(payload);

      // Invalida la lista e i contatori dei download
      queryClient.invalidateQueries({ queryKey: ["downloads"] });
      queryClient.invalidateQueries({ queryKey: ["downloads-summary"] });
    };

    // Gli errori di rete li ritenta il browser. Una risposta non-200 (es. 401
    // col cookie di stream scaduto, o il server che riparte) chiude lo
    // stream: si rinnova la sessione e si riapre, con un'attesa che raddoppia
    // a ogni tentativo. Se la sessione non si rinnova, l'utente è uscito e lo
    // stream si riapre al prossimo login.
    const scheduleReconnect = () => {
      const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** attempts);
      attempts += 1;
      reconnect = setTimeout(async () => {
        if (authEnabled && !(await refreshAccessToken())) return;
        if (!disposed) open();
      }, delay);
    };

    const open = () => {
      const current = createProgressSource();
      source = current;
      current.onopen = () => {
        attempts = 0;
      };
      current.onmessage = onMessage;
      current.onerror = () => {
        if (current.readyState !== EventSource.CLOSED) return;
        source = null;
        scheduleReconnect();
      };
    };

    open();
    return () => {
      disposed = true;
      clearTimeout(reconnect);
      source?.close();
    };
  }, [queryClient, canListen, authEnabled, user?.id, store]);

  const addItems = useCallback(
    (_ids: string[]) => {
      _ids.forEach((id) => activeIdsRef.current.add(id));
      store.activeCount = activeIdsRef.current.size;
      store.notify();
      queryClient.invalidateQueries({ queryKey: ["downloads"] });
      queryClient.invalidateQueries({ queryKey: ["downloads-summary"] });
    },
    [queryClient, store],
  );

  // Stable: consumers are never re-rendered by the context itself.
  const value = useMemo(() => ({ store, addItems }), [store, addItems]);
  return <QueueContext.Provider value={value}>{children}</QueueContext.Provider>;
}

/** Start-download notification, without subscribing to live states. */
export function useQueueActions() {
  return useContext(QueueContext).addItems;
}

/**
 * Calls `listener` for every event of the progress stream the app already
 * has open (one connection for the whole app), for as long as the component
 * is mounted.
 */
export function useProgressEvents(listener: (event: StreamEvent) => void) {
  const { store } = useContext(QueueContext);
  const latest = useRef(listener);
  useEffect(() => {
    latest.current = listener;
  });
  useEffect(() => store.onEvent((event) => latest.current(event)), [store]);
}

/**
 * The live state of one download: by the track's canonical id, or by the id
 * of the download a component started itself. Re-renders only when that
 * entry changes.
 */
export function useLiveState(trackId?: string | null, downloadId?: string | null): LiveTrackState | undefined {
  const { store } = useContext(QueueContext);
  const read = () => (trackId ? store.byTrack.get(trackId) : undefined) ?? (downloadId ? store.byDownload.get(downloadId) : undefined);
  return useSyncExternalStore(store.subscribe, read, read);
}

/**
 * Every live state (snapshot copies): for the few views that show many
 * downloads at once. Re-renders on every event.
 */
export function useQueue() {
  const { store, addItems } = useContext(QueueContext);
  const version = useSyncExternalStore(store.subscribe, () => store.version, () => store.version);
  return useMemo(
    () => ({
      totalActive: store.activeCount,
      liveStates: Object.fromEntries(store.byTrack) as Record<string, LiveTrackState>,
      liveStatesByDownload: Object.fromEntries(store.byDownload) as Record<string, LiveTrackState>,
      addItems,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [version, store, addItems],
  );
}
