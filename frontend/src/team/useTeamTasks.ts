import { useEffect, useMemo, useSyncExternalStore } from 'react';
import type { Actor, SessionView, TeamApi } from './api.ts';
import { createTeamTaskStore } from './taskStore.ts';

/** StrictMode cleanup detaches listeners; submitted server commands keep running. */
export function useTeamTasks(api: TeamApi, session: Actor | SessionView | null) {
  const store = useMemo(() => createTeamTaskStore(api,{autoSubscribe:false}),[api]);
  useEffect(() => {
    const disconnect = store.connect();
    store.setSession(api.actor ? session ?? api.actor : null);
    return disconnect;
  },[store,api,session]);
  const state = useSyncExternalStore(store.subscribe,store.getSnapshot,store.getSnapshot);
  return {state,store};
}
