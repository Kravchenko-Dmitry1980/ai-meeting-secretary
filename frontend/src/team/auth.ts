import { TeamApiError } from './api.ts';
import type { Actor, TeamApi } from './api';

export type TeamAuthSnapshot = Readonly<{
  status: 'checking' | 'anonymous' | 'authenticated' | 'error' | 'uncertain';
  actor: Actor | null;
  error: TeamApiError | null;
  sessionEpoch: number;
}>;
export interface TeamAuth {
  getSnapshot(): TeamAuthSnapshot;
  subscribe(listener: () => void): () => void;
  bootstrap(options?: { max?: unknown }): Promise<void>;
  loginCode(value: string): Promise<void>;
  logout(): Promise<void>;
  dispose(): void;
}

/** Official MAX Bridge contract: https://dev.max.ru/docs/webapps/bridge.
 * The deployment must supply a vetted bridge. This module never loads scripts,
 * parses URL credentials or trusts initDataUnsafe to identify a member.
 */
export function getMaxInitData(source: unknown = typeof window === 'undefined' ? null : window): string | null {
  try {
    if (source === null || typeof source !== 'object' || !('WebApp' in source)) return null;
    const bridge = source.WebApp;
    if (bridge === null || typeof bridge !== 'object' || !('initData' in bridge)) return null;
    const value = bridge.initData;
    if (value === '' || value == null) return null;
    if (typeof value !== 'string' || value.includes('\0') || value.length > 16384
      || new TextEncoder().encode(value).length > 16384 || /[\ud800-\udfff]/.test(value)) throw new Error();
    return value;
  } catch { throw new TeamApiError('team_init_data_invalid', 0); }
}

export type TeamAuthBootstrapOptions = Readonly<{ max?: unknown; signal?: AbortSignal }>;

export function createTeamAuthLifecycle(auth: TeamAuth, options: TeamAuthBootstrapOptions = {}): () => () => void {
  let generation = 0;
  const max = options.max, signal = options.signal;
  return () => {
    const mountedGeneration = ++generation;
    const cancelled = () => auth.dispose();
    if (signal?.aborted) { cancelled(); return () => {}; }
    signal?.addEventListener('abort', cancelled, { once: true });
    void auth.bootstrap({ max }).catch(() => {});
    return () => {
      signal?.removeEventListener('abort', cancelled);
      // React's development StrictMode restores the same effect immediately.
      // Only a cleanup that remains current after that turn owns final disposal.
      queueMicrotask(() => { if (generation === mountedGeneration) auth.dispose(); });
    };
  };
}

export function createTeamAuth(api: TeamApi): TeamAuth {
  let snapshot: TeamAuthSnapshot = Object.freeze({ status: api.actor ? 'authenticated' : 'checking',
    actor: api.actor, error: null, sessionEpoch: api.sessionEpoch });
  let pending: Promise<void> | null = null, disposed = false, maxAttempted = false;
  const listeners = new Set<() => void>();
  const publish = (status: TeamAuthSnapshot['status'], error: TeamApiError | null = null) => {
    if (disposed) return;
    snapshot = Object.freeze({ status, actor: api.actor, error, sessionEpoch: api.sessionEpoch });
    for (const listener of listeners) listener();
  };
  let unsubscribe: (() => void) | null = null;
  const connect = () => {
    if (!unsubscribe && !disposed) unsubscribe = api.subscribeSession((actor) => publish(actor ? 'authenticated' : 'anonymous'));
  };
  const normalize = (error: unknown) => error instanceof TeamApiError ? error : new TeamApiError('team_auth_unavailable', 0);
  const failed = (error: TeamApiError) => publish(error.uncertain ? 'uncertain' : error.status === 401 ? 'anonymous' : 'error', error);
  const once = (operation: () => Promise<void>): Promise<void> => {
    if (disposed) return Promise.reject(new TeamApiError('team_auth_disposed', 0));
    connect();
    if (pending) return pending;
    // Start immediately, so a click cannot race a second credential attempt.
    pending = operation().finally(() => { pending = null; });
    return pending;
  };
  return {
    getSnapshot: () => snapshot,
    subscribe(listener) { connect(); listeners.add(listener); return () => { listeners.delete(listener); }; },
    bootstrap(options = {}) {
      return once(async () => {
        publish('checking');
        try { await api.me(); publish('authenticated'); return; }
        catch (error) {
          const safe = normalize(error);
          if (safe.status !== 401) { failed(safe); return; }
        }
        if (disposed) return;
        try {
          const initData = getMaxInitData(options.max);
          if (maxAttempted || initData === null) { publish('anonymous'); return; }
          maxAttempted = true;
          await api.loginMax(initData);
          publish('authenticated');
        } catch (error) { failed(normalize(error)); }
      });
    },
    loginCode(value) {
      return once(async () => {
        publish('checking');
        try { await api.loginCode(value); publish('authenticated'); }
        catch (error) { const safe = normalize(error); failed(safe); throw safe; }
      });
    },
    logout() {
      return once(async () => {
        try { await api.logout(); publish('anonymous'); }
        catch (error) { const safe = normalize(error); failed(safe); throw safe; }
      });
    },
    dispose() { disposed = true; unsubscribe?.(); unsubscribe = null; listeners.clear(); },
  };
}
