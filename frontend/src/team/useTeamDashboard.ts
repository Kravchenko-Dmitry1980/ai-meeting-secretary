import { useEffect, useMemo, useSyncExternalStore } from 'react';
import type { TeamApi } from './api';
import { createTeamDashboardStore } from './dashboardStore';
import type { DashboardConfiguration } from './dashboardStore';

export function useTeamDashboard(api: TeamApi,configuration: DashboardConfiguration) {
  const store=useMemo(()=>createTeamDashboardStore(api),[api]);
  const {actor,sessionEpoch,projectId,expanded}=configuration;
  useEffect(()=>store.connect(),[store]);
  useEffect(()=>{
    store.configure({actor,sessionEpoch,projectId,expanded});
    if(expanded)void store.refresh();
  },[store,actor,sessionEpoch,projectId,expanded]);
  useEffect(()=>{
    if(!expanded)return;
    const timer=window.setInterval(()=>{
      if(document.visibilityState==='visible')void store.autoRefresh();
    },30_000);
    return()=>window.clearInterval(timer);
  },[store,expanded]);
  const state=useSyncExternalStore(store.subscribe,store.getSnapshot,store.getSnapshot);
  return{state,store};
}
