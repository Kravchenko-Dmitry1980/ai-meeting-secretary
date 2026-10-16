import assert from 'node:assert/strict';
import test from 'node:test';

const OWNER = { id:'00000000-0000-4000-8000-000000000001',display_name:'Дмитрий',role:'owner',project_ids:['7','8'],revision:2 };
const NOW = '2026-10-05T09:00:00.000Z';
const OP1 = '00000000-0000-4000-8000-000000000003';
const OP2 = '00000000-0000-4000-8000-000000000004';
const OP3 = '00000000-0000-4000-8000-000000000005';

function deferred() { let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject}; }
function command(operation_id=OP1,state='queued') {
  return {operation_id,task_id:'9007199254740993',action:'set_state',state,accepted_at:NOW,last_state_at:NOW,lease_until:null,error_code:null};
}
function dashboard(project_id='7',items=[command()],next_cursor=null) {
  return {project_id,status:{project_id,sync:{state:'not_configured',issue_count:0},cloud:{state:'not_configured'}},commands:{items,next_cursor},
    deliveries:{state:'not_configured',sources:[],pending_count:null,sending_count:null,retryable_count:null,uncertain_count:null,
      rejected_count:null,max_api_accepted_count:null,last_max_api_accepted_at:null,human_read_confirmed:null},
    webhook:{state:'unknown',last_authenticated_accept_at:null},
    notifications:{state:'not_configured',pending_count:null,sending_count:null,uncertain_count:null,failed_count:null,cancelled_count:null,
      last_max_api_accepted_at:null,human_read_confirmed:null}};
}
async function setup(getDashboard=async(id)=>dashboard(id),options={}) {
  const {createTeamDashboardStore}=await import('../src/team/dashboardStore.ts');
  const calls=[],listeners=new Set();
  const api={actor:OWNER,sessionEpoch:1,subscribeSession:(callback)=>{listeners.add(callback);return()=>listeners.delete(callback);},
    clearSession(){this.actor=null;this.sessionEpoch++;for(const callback of listeners)callback(this.actor,this.sessionEpoch);},
    async getDashboard(projectId,readOptions){calls.push({projectId,options:readOptions});return getDashboard(projectId,readOptions);}};
  const store=createTeamDashboardStore(api,{clock:()=>Date.parse(NOW),...options});
  const disconnect=store.connect();
  store.configure({actor:OWNER,sessionEpoch:1,projectId:'7',expanded:true});
  return{store,api,calls,disconnect};
}

test('owner dashboard stays closed/member/out of scope without any GET or fabricated zero',async()=>{
  const c=await setup();
  c.store.configure({actor:OWNER,sessionEpoch:1,projectId:'7',expanded:false});
  await c.store.refresh();await c.store.autoRefresh();
  c.store.configure({actor:{...OWNER,role:'member'},sessionEpoch:1,projectId:'7',expanded:true});
  await c.store.refresh();
  c.store.configure({actor:OWNER,sessionEpoch:1,projectId:'9',expanded:true});await c.store.refresh();
  assert.equal(c.calls.length,0);assert.equal(c.store.getSnapshot().data,null);assert.deepEqual(c.store.getSnapshot().commands,[]);
  c.disconnect();
});

test('one bounded read yields actual nullable counts and exact task IDs',async()=>{
  const c=await setup();await c.store.refresh();
  assert.equal(c.calls.length,1);assert.equal(c.calls[0].options.limit,25);assert.ok(c.calls[0].options.signal instanceof AbortSignal);
  const state=c.store.getSnapshot();assert.equal(state.data.deliveries.pending_count,null);
  assert.equal(state.commands[0].task_id,'9007199254740993');assert.equal(state.lastSuccessfulAt,NOW);assert.equal(state.errorCode,null);
  c.disconnect();
});

test('a late old-project GET cannot paint the new project, even when abort is ignored',async()=>{
  const old=deferred();const c=await setup(id=>id==='7'?old.promise:Promise.resolve(dashboard(id,[command(OP2)])));
  const first=c.store.refresh();c.store.configure({actor:OWNER,sessionEpoch:1,projectId:'8',expanded:true});
  await c.store.refresh();old.resolve(dashboard());await first;
  assert.equal(c.calls[0].options.signal.aborted,true);assert.equal(c.store.getSnapshot().data.project_id,'8');
  assert.deepEqual(c.store.getSnapshot().commands.map(row=>row.operation_id),[OP2]);c.disconnect();
});

test('same owner re-login fences old data by session epoch before later response',async()=>{
  const old=deferred();const c=await setup(()=>old.promise);const first=c.store.refresh();
  c.api.sessionEpoch=2;c.store.configure({actor:OWNER,sessionEpoch:2,projectId:'7',expanded:true});
  old.resolve(dashboard());await first;assert.equal(c.store.getSnapshot().data,null);assert.equal(c.calls.length,1);
  assert.equal(c.store.getSnapshot().scope.sessionEpoch,2);c.disconnect();
});

test('logout or disconnect stops polling and rejects late noncooperative responses',async()=>{
  const old=deferred();const c=await setup(()=>old.promise);const first=c.store.refresh();
  c.api.clearSession();old.resolve(dashboard());await first;await c.store.autoRefresh();
  assert.equal(c.store.getSnapshot().data,null);assert.equal(c.calls.length,1);assert.equal(c.calls[0].options.signal.aborted,true);
  c.disconnect();await c.store.refresh();assert.equal(c.calls.length,1);
});

test('StrictMode disconnect/reconnect cannot revive the cancelled first GET',async()=>{
  const old=deferred();let attempt=0;
  const c=await setup(()=>++attempt===1?old.promise:Promise.resolve(dashboard('7',[command(OP2)])));
  const first=c.store.refresh();c.disconnect();
  const disconnect=c.store.connect();c.store.configure({actor:OWNER,sessionEpoch:1,projectId:'7',expanded:true});
  await c.store.refresh();old.resolve(dashboard());await first;
  assert.equal(c.calls.length,2);assert.equal(c.calls[0].options.signal.aborted,true);
  assert.deepEqual(c.store.getSnapshot().commands.map(row=>row.operation_id),[OP2]);
  disconnect();await c.store.autoRefresh();assert.equal(c.calls.length,2);
});

test('closing hides old snapshot and reopening asks for fresh data',async()=>{
  const c=await setup();await c.store.refresh();c.store.configure({actor:OWNER,sessionEpoch:1,projectId:'7',expanded:false});
  assert.equal(c.store.getSnapshot().data,null);assert.equal(c.store.getSnapshot().lastSuccessfulAt,null);
  await c.store.autoRefresh();assert.equal(c.calls.length,1);
  c.store.configure({actor:OWNER,sessionEpoch:1,projectId:'7',expanded:true});await c.store.refresh();assert.equal(c.calls.length,2);c.disconnect();
});

test('double refresh shares one in-flight GET',async()=>{
  const read=deferred();const c=await setup(()=>read.promise);const first=c.store.refresh(),second=c.store.refresh();
  assert.equal(first,second);assert.equal(c.calls.length,1);read.resolve(dashboard());await first;c.disconnect();
});

test('failed read clears unknown current counts and pauses auto until explicit retry',async()=>{
  let failed=false;const c=await setup(async()=>{if(failed)throw Object.assign(new Error('private body'),{code:'team_dashboard_unavailable',status:503});return dashboard();});
  await c.store.refresh();failed=true;await c.store.autoRefresh();
  const state=c.store.getSnapshot();assert.equal(state.data,null);assert.equal(state.errorCode,'team_dashboard_unavailable');assert.equal(state.autoPaused,true);
  assert.equal(state.lastSuccessfulAt,NOW);await c.store.autoRefresh();await c.store.autoRefresh();assert.equal(c.calls.length,2);
  failed=false;await c.store.refresh();assert.equal(c.calls.length,3);assert.equal(c.store.getSnapshot().autoPaused,false);c.disconnect();
});

test('explicit pages preserve distinct operation IDs and refuse repeating cursor',async()=>{
  const c=await setup(async(id,options)=>!options.after?dashboard(id,[command(OP1)],'page_A'):dashboard(id,[command(OP1,'running'),command(OP2,'uncertain')],'page_B'));
  await c.store.refresh();await c.store.loadMore();
  assert.equal(c.calls.length,2);assert.equal(c.calls[1].options.after,'page_A');assert.deepEqual(c.store.getSnapshot().commands.map(row=>row.operation_id),[OP1,OP2]);
  await c.store.loadMore();assert.equal(c.store.getSnapshot().pageErrorCode,'team_dashboard_cursor_invalid');assert.equal(c.store.getSnapshot().autoPaused,true);
  assert.deepEqual(c.store.getSnapshot().commands.map(row=>row.operation_id),[OP1,OP2]);c.disconnect();
});

test('pagination has a finite page budget and does not claim exhaustive commands',async()=>{
  let page=0;const c=await setup(async(id)=>{page++;return dashboard(id,[command([OP1,OP2,OP3][page-1])],`page_${page}`);},{maxPages:2});
  await c.store.refresh();await c.store.loadMore();await c.store.loadMore();
  assert.equal(c.calls.length,2);assert.equal(c.store.getSnapshot().paginationLimited,true);assert.equal(c.store.getSnapshot().nextCursor,null);c.disconnect();
});

test('invalid cross-project status or oversized page cannot become dashboard evidence',async()=>{
  const wrong=dashboard();wrong.status.project_id='8';const c=await setup(async()=>wrong);await c.store.refresh();
  assert.equal(c.store.getSnapshot().data,null);assert.equal(c.store.getSnapshot().errorCode,'team_dashboard_response_invalid');
  c.api.getDashboard=async()=>dashboard('7',Array.from({length:26},(_,index)=>command(`00000000-0000-4000-8000-${String(index).padStart(12,'0')}`)));
  await c.store.refresh();assert.equal(c.store.getSnapshot().data,null);assert.equal(c.store.getSnapshot().errorCode,'team_dashboard_response_invalid');c.disconnect();
});

test('401 revokes session while 403 remains a scoped dashboard error',async()=>{
  const c=await setup(async()=>{throw Object.assign(new Error('no'),{code:'team_owner_required',status:403});});await c.store.refresh();
  assert.equal(c.api.actor,OWNER);assert.equal(c.store.getSnapshot().errorCode,'team_owner_required');
  c.api.getDashboard=async()=>{throw Object.assign(new Error('no'),{code:'team_session_expired',status:401});};await c.store.refresh();
  assert.equal(c.api.actor,null);assert.equal(c.store.getSnapshot().data,null);await c.store.autoRefresh();assert.equal(c.calls.length,1);c.disconnect();
});
