import { useEffect, useMemo, useSyncExternalStore } from 'react';
import type { TeamApi } from './api';
import { createTeamAuth, createTeamAuthLifecycle } from './auth';
import type { TeamAuthBootstrapOptions } from './auth';

export function useTeamAuth(api: TeamApi, options: TeamAuthBootstrapOptions = {}) {
  const auth = useMemo(() => createTeamAuth(api), [api]);
  const max = options.max, signal = options.signal;
  const lifecycle = useMemo(() => createTeamAuthLifecycle(auth, { max, signal }), [auth, max, signal]);
  const state = useSyncExternalStore(auth.subscribe, auth.getSnapshot, auth.getSnapshot);
  useEffect(lifecycle, [lifecycle]);
  return { state, auth };
}
