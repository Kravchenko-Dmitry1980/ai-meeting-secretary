/** Owner diagnostics are read-only, bounded and discarded across every auth/scope boundary. */
import type { Actor, OwnerDashboardView, TeamApi } from './api.ts';

type DashboardCommand = OwnerDashboardView['commands']['items'][number];
type DashboardApi = Pick<TeamApi, 'actor' | 'sessionEpoch' | 'subscribeSession' | 'clearSession' | 'getDashboard'>;
export interface DashboardScope { actorId: string; actorRevision: number; sessionEpoch: number; projectId: string }
export interface DashboardConfiguration { actor: Actor | null; sessionEpoch: number; projectId: string | null; expanded: boolean }
export interface DashboardState {
  scope: DashboardScope | null;
  expanded: boolean;
  data: OwnerDashboardView | null;
  commands: readonly DashboardCommand[];
  nextCursor: string | null;
  loading: boolean;
  pageLoading: boolean;
  errorCode: string | null;
  pageErrorCode: string | null;
  lastSuccessfulAt: string | null;
  autoPaused: boolean;
  paginationLimited: boolean;
  pagesLoaded: number;
}
interface Options { clock?: () => number; maxPages?: number }
const PAGE_SIZE = 25;
const initial = (): DashboardState => ({ scope:null,expanded:false,data:null,commands:Object.freeze([]),nextCursor:null,
  loading:false,pageLoading:false,errorCode:null,pageErrorCode:null,lastSuccessfulAt:null,autoPaused:false,
  paginationLimited:false,pagesLoaded:0 });
function sameScope(left: DashboardScope | null,right: DashboardScope | null): boolean {
  return left === right || !!left && !!right && left.actorId===right.actorId && left.actorRevision===right.actorRevision
    && left.sessionEpoch===right.sessionEpoch && left.projectId===right.projectId;
}
function immutable<T>(value: T): T {
  if (value && typeof value==='object') { for(const child of Object.values(value))immutable(child);Object.freeze(value); }
  return value;
}
function code(error: unknown): string {
  return error && typeof error==='object' && 'code' in error && typeof error.code==='string'
    && /^[a-z][a-z0-9_]{0,100}$/.test(error.code) ? error.code : 'team_dashboard_unavailable';
}
function invalid(errorCode: string): Error { return Object.assign(new Error(errorCode),{code:errorCode}); }

export class TeamDashboardStore {
  private state: DashboardState = Object.freeze(initial());
  private configuration: DashboardConfiguration = {actor:null,sessionEpoch:-1,projectId:null,expanded:false};
  private listeners = new Set<() => void>();
  private connected = false;
  private disconnect: (() => void) | null = null;
  private generation = 0;
  private connection = 0;
  private request: AbortController | null = null;
  private pending: Promise<void> | null = null;
  private cursors = new Set<string>();
  private clock: () => number;
  private maxPages: number;
  readonly api: DashboardApi;

  constructor(api: DashboardApi,options: Options={}) {
    this.api=api;this.clock=options.clock??Date.now;
    this.maxPages=options.maxPages??10;
    if(!Number.isSafeInteger(this.maxPages)||this.maxPages<1||this.maxPages>20)throw new Error('team_dashboard_page_budget_invalid');
  }
  getSnapshot = (): DashboardState => this.state;
  subscribe = (listener: () => void): (() => void) => {this.listeners.add(listener);return()=>this.listeners.delete(listener);};
  private update(patch: Partial<DashboardState>) {
    this.state=Object.freeze({...this.state,...patch});
    for(const listener of this.listeners)listener();
  }
  private cancel() {this.generation++;this.request?.abort();this.request=null;this.pending=null;this.cursors.clear();}
  connect = (): (() => void) => {
    this.disconnect?.();this.connected=true;const connection=++this.connection;
    const unsubscribe=this.api.subscribeSession(()=>this.configure(this.configuration));
    const stop=()=>{
      unsubscribe();
      if(connection!==this.connection)return;
      this.connected=false;this.cancel();this.disconnect=null;this.state=Object.freeze(initial());
      for(const listener of this.listeners)listener();
    };
    this.disconnect=stop;return stop;
  };
  configure = (configuration: DashboardConfiguration): void => {
    this.configuration=configuration;
    const actor=configuration.actor,actual=this.api.actor,projectId=configuration.projectId;
    const permitted=actor?.role==='owner' && actual?.role==='owner' && actor.id===actual.id && actor.revision===actual.revision
      && configuration.sessionEpoch===this.api.sessionEpoch && typeof projectId==='string'
      && /^[1-9][0-9]{0,127}$/.test(projectId) && actor.project_ids.includes(projectId) && actual.project_ids.includes(projectId);
    const scope: DashboardScope|null=permitted?Object.freeze({actorId:actor.id,actorRevision:actor.revision,sessionEpoch:configuration.sessionEpoch,projectId}):null;
    const expanded=!!scope&&configuration.expanded;
    if(sameScope(scope,this.state.scope)&&expanded===this.state.expanded)return;
    this.cancel();this.state=Object.freeze({...initial(),scope,expanded});
    for(const listener of this.listeners)listener();
  };
  private allowed(): boolean {
    const scope=this.state.scope,actor=this.api.actor;
    return this.connected && this.state.expanded && !!scope && actor?.role==='owner' && actor.id===scope.actorId
      && actor.revision===scope.actorRevision && actor.project_ids.includes(scope.projectId) && this.api.sessionEpoch===scope.sessionEpoch;
  }
  refresh = (): Promise<void> => this.start(false);
  autoRefresh = (): Promise<void> => this.state.autoPaused ? Promise.resolve() : this.start(false);
  loadMore = (): Promise<void> => {
    if(!this.state.nextCursor)return Promise.resolve();
    if(this.state.pagesLoaded>=this.maxPages){this.update({nextCursor:null,paginationLimited:true});return Promise.resolve();}
    return this.start(true);
  };
  private start(more: boolean): Promise<void> {
    if(!this.allowed())return Promise.resolve();
    if(this.pending)return this.pending;
    const scope=this.state.scope!;
    const generation=this.generation;
    const controller=new AbortController();this.request=controller;
    const after=more?this.state.nextCursor:null;
    if(more&&!after)return Promise.resolve();
    this.update(more?{pageLoading:true,pageErrorCode:null}:{loading:true,errorCode:null,pageErrorCode:null,autoPaused:false});
    const pending=this.read(scope,generation,controller,after,more).finally(()=>{
      if(this.request===controller){this.request=null;this.pending=null;}
    });
    this.pending=pending;return pending;
  }
  private live(scope: DashboardScope,generation: number,controller: AbortController): boolean {
    return !controller.signal.aborted && generation===this.generation && sameScope(scope,this.state.scope) && this.allowed();
  }
  private async read(scope: DashboardScope,generation: number,controller: AbortController,after: string|null,more: boolean) {
    try {
      const response=await this.api.getDashboard(scope.projectId,{limit:PAGE_SIZE,...(after?{after}:{}),signal:controller.signal});
      if(!this.live(scope,generation,controller))return;
      if(response.project_id!==scope.projectId || response.status.project_id!==scope.projectId
          || !Array.isArray(response.commands.items) || response.commands.items.length>PAGE_SIZE
          || new Set(response.commands.items.map(item=>item.operation_id)).size!==response.commands.items.length)throw invalid('team_dashboard_response_invalid');
      const cursor=response.commands.next_cursor??null;
      if(cursor && (!/^[A-Za-z0-9_-]{1,512}$/.test(cursor)||cursor===after||more&&this.cursors.has(cursor)))throw invalid('team_dashboard_cursor_invalid');
      if(!more)this.cursors.clear();
      if(after)this.cursors.add(after);
      const rows=new Map<string,DashboardCommand>(more?this.state.commands.map(item=>[item.operation_id,item]):[]);
      for(const item of response.commands.items)rows.set(item.operation_id,immutable(structuredClone(item)));
      const pagesLoaded=more?this.state.pagesLoaded+1:1;
      const paginationLimited=!!cursor&&pagesLoaded>=this.maxPages;
      this.update({data:immutable(structuredClone(response)),commands:Object.freeze([...rows.values()]),
        nextCursor:paginationLimited?null:cursor,paginationLimited,pagesLoaded,lastSuccessfulAt:new Date(this.clock()).toISOString(),
        errorCode:null,pageErrorCode:null,autoPaused:false});
    } catch(error) {
      if(!this.live(scope,generation,controller))return;
      if(error&&typeof error==='object'&&'status' in error&&error.status===401){this.api.clearSession();this.configure({...this.configuration,actor:null,expanded:false});return;}
      this.update(more?{pageErrorCode:code(error),autoPaused:true}
        :{data:null,commands:[],nextCursor:null,pagesLoaded:0,paginationLimited:false,errorCode:code(error),autoPaused:true});
    } finally {
      if(this.live(scope,generation,controller))this.update(more?{pageLoading:false}:{loading:false});
    }
  }
}
export function createTeamDashboardStore(api: DashboardApi,options: Options={}): TeamDashboardStore {return new TeamDashboardStore(api,options);}
