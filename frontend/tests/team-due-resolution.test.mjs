import assert from 'node:assert/strict';
import test from 'node:test';
import { createTeamApi, TeamApiError } from '../src/team/api.ts';
import { TeamDueResolutionStore, utcFromMoscow } from '../src/team/dueResolutionStore.ts';
const OWNER='00000000-0000-4000-8000-000000000001', OBS='00000000-0000-4000-8000-000000000002';
const PREVIEW='00000000-0000-4000-8000-000000000003', OP='00000000-0000-4000-8000-000000000004';
const PROJECT='9007199254740993', TASK='9007199254740995';
const actor={id:OWNER,revision:0,role:'owner',project_ids:[PROJECT,'7'],display_name:'Synthetic owner'};
const candidate=()=>({observation_id:OBS,project_id:PROJECT,task_id:TASK,baseline_revision:2,
  baseline_fingerprint:'a'.repeat(64),observed_fingerprint:'b'.repeat(64),baseline_due_at:null,
  observed_due_at:'2030-10-04T10:00:00Z',title:'Synthetic <literal> task'});
const task=()=>({task_id:TASK,project_id:PROJECT,revision:2,remote_fingerprint:'a'.repeat(64),title:'Synthetic task'});
const preview=()=>({preview_id:PREVIEW,candidate:candidate(),due_at:null,reason:'Explicit owner reason',
  created_at:'2030-10-04T10:00:00Z',expires_at:'2030-10-04T10:05:00Z'});
const receipt=()=>({acceptance_receipt:{operation_id:OP,payload_hash:'c'.repeat(64),decision:'accepted',
  decided_at:'2030-10-04T10:01:00Z',error_code:null},execution_state:{state:'queued',revision:0,
  task_id:TASK,verified_at:null,error_code:null,retry_after:null},current:task()});
const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const session=()=>({actor,csrf:'synthetic_csrf_1234567890123456789012345678'});
async function client(handler) {
  const calls=[];
  const api=createTeamApi({fetcher:async(url,init)=>{calls.push({url,init});return url.endsWith('/me')?json(session()):handler(url,init);}});
  await api.me();return{api,calls};
}
function storeFixture(overrides={}) {
  const api={actor,sessionEpoch:1,getDueResolution:async()=>candidate(),createDueResolutionPreview:async()=>preview(),
    confirmDueResolution:async()=>receipt(),getReceipt:async()=>receipt(),...overrides};
  const store=new TeamDueResolutionStore(api,{uuid:()=>OP,now:()=>0});
  store.setScope(actor,1,PROJECT,task());return{api,store};
}
test('owner client binds exact source/date/reason and confirms only immutable preview route with CSRF',async()=>{
  const {api,calls}=await client((url)=>url.endsWith('/previews')?json(preview(),201):url.endsWith('/confirm')?json(receipt(),202):json(candidate()));
  const source=await api.getDueResolution(TASK);
  const frozen=await api.createDueResolutionPreview(source,{due_at:null,reason:'Explicit owner reason'});
  await api.confirmDueResolution(frozen,OP);
  assert.deepEqual(calls.slice(1).map(row=>row.url),[`/api/team/v1/tasks/${TASK}/due-resolution`,
    `/api/team/v1/tasks/${TASK}/due-resolution/previews`,`/api/team/v1/due-resolutions/${PREVIEW}/confirm`]);
  assert.deepEqual(JSON.parse(calls[2].init.body),{observation_id:OBS,expected_revision:2,expected_fingerprint:'b'.repeat(64),due_at:null,reason:'Explicit owner reason'});
  assert.deepEqual(JSON.parse(calls[3].init.body),{operation_id:OP});
  assert.equal(calls[3].init.headers.get('X-CSRF-Token'),session().csrf);
  assert.equal(calls[3].init.credentials,'same-origin');
});
test('due API rejects omitted date, wrong source identity, private output and raw resolve_due commands',async()=>{
  const {api,calls}=await client(()=>json({...candidate(),before_image:{private:'never accepted'}}));
  await assert.rejects(api.getDueResolution(TASK),error=>error.code==='team_response_invalid');
  for(const choice of [{reason:'missing date'},{due_at:null,reason:' '},{due_at:'2030-10-04T13:00:00+03:00',reason:'offset'}])
    await assert.rejects(api.createDueResolutionPreview(candidate(),choice),error=>error.code==='team_request_invalid');
  const before=calls.length;
  await assert.rejects(api.postCommand({operation_id:OP,project_id:PROJECT,task_id:TASK,action:'resolve_due',values:{due_at:null}}),error=>error.code==='team_request_invalid');
  assert.equal(calls.length,before);
  const other=await client(()=>json({...preview(),candidate:{...candidate(),observed_fingerprint:'d'.repeat(64)}},201));
  await assert.rejects(other.api.createDueResolutionPreview(candidate(),{due_at:null,reason:'Explicit owner reason'}),error=>error.code==='team_response_invalid'&&error.uncertain);
});
test('member cannot dispatch due resolution requests and explicit403 does not consume session',async()=>{
  let calls=0;
  const api=createTeamApi({fetcher:async()=>{calls+=1;return json({actor:{...actor,role:'member'},csrf:session().csrf});}});
  await api.me();
  await assert.rejects(api.getDueResolution(TASK),error=>error.status===403);
  assert.equal(calls,1);assert.equal(api.actor.role,'member');
});
test('a queued receipt cannot claim another task when current snapshot is absent',async()=>{
  const wrong={...receipt(),current:null,execution_state:{...receipt().execution_state,task_id:'7'}};
  const {api}=await client(()=>json(wrong,202));
  await assert.rejects(api.confirmDueResolution(preview(),OP),error=>error.code==='team_response_invalid'&&error.uncertain);
  const fixture=storeFixture({getReceipt:async()=>wrong});
  await fixture.store.load();await fixture.store.prepare(null,'Explicit owner reason');await fixture.store.confirm();
  await fixture.store.poll(OP);
  assert.equal(fixture.store.getSnapshot().operations[0].errorCode,'team_response_invalid');
});
test('late candidate GET is fenced by project/session epoch and dismissal even if mock ignores abort',async()=>{
  for(const change of ['project','session','dismiss']) {
    let finish;
    const {api,store}=storeFixture({getDueResolution:()=>new Promise(resolve=>{finish=resolve;})});
    const pending=store.load();
    if(change==='project')store.setScope(actor,1,'7',null);
    if(change==='session'){api.sessionEpoch=2;store.setScope(actor,2,PROJECT,task());}
    if(change==='dismiss')store.close();
    finish(candidate());await pending;
    assert.equal(store.getSnapshot().candidate,null);assert.equal(store.getSnapshot().busy,false);
  }
});
test('preview requires explicit choice, and changed baseline never becomes a resolvable candidate',async()=>{
  const {store}=storeFixture();await store.load();
  await assert.rejects(store.prepare(undefined,'Reason'),error=>error.code==='team_due_required');
  await assert.rejects(store.prepare(null,' '),error=>error.code==='team_due_required');
  const stale=storeFixture({getDueResolution:async()=>({...candidate(),baseline_revision:3})});
  await stale.store.load();assert.equal(stale.store.getSnapshot().candidate,null);
  assert.equal(stale.store.getSnapshot().errorCode,'team_preview_stale');
});
test('double confirm shares one POST; closing/changing task preserves its pending receipt without optimistic apply',async()=>{
  let finish,calls=0;
  const {store}=storeFixture({confirmDueResolution:()=>{calls+=1;return new Promise(resolve=>{finish=resolve;});}});
  await store.load();await store.prepare(null,'Explicit owner reason');
  const first=store.confirm();assert.equal(store.confirm(),first);assert.equal(calls,1);
  store.close();store.setScope(actor,1,PROJECT,null);
  finish(receipt());await first;
  assert.equal(store.getSnapshot().operations[0].receipt.execution_state.state,'queued');
  assert.equal(store.getSnapshot().operations[0].operationId,OP);
  assert.equal(store.getSnapshot().candidate,null);
});
test('uncertain confirmation never replays POST; explicit GET checks the stable operation ID',async()=>{
  let posts=0,reads=0;
  const {store}=storeFixture({confirmDueResolution:async()=>{posts+=1;throw new TeamApiError('team_transport_unavailable',0,true);},
    getReceipt:async id=>{reads+=1;assert.equal(id,OP);return receipt();}});
  await store.load();await store.prepare(null,'Explicit owner reason');await store.confirm();await store.confirm();
  assert.equal(posts,1);assert.equal(store.getSnapshot().operations[0].transport,'unknown');
  await store.poll(OP);assert.equal(reads,1);assert.equal(posts,1);
  assert.equal(store.getSnapshot().operations[0].receipt.execution_state.state,'queued');
});
test('a pending or uncertain decision blocks new preview preparation for the same task',async()=>{
  let reads=0;
  const {store}=storeFixture({getDueResolution:async()=>{reads+=1;return candidate();},
    confirmDueResolution:async()=>{throw new TeamApiError('team_transport_unavailable',0,true);}});
  await store.load();await store.prepare(null,'Explicit owner reason');await store.confirm();store.close();
  await assert.rejects(store.load(),error=>error.code==='team_operation_pending');
  assert.equal(reads,1);
});
test('expired preview and disconnected owner cannot dispatch confirmation',async()=>{
  let posts=0;
  const {store}=storeFixture({createDueResolutionPreview:async()=>({...preview(),expires_at:'1969-12-31T23:59:59Z'}),
    confirmDueResolution:async()=>{posts+=1;return receipt();}});
  await store.load();await store.prepare(null,'Reason');
  await assert.rejects(store.confirm(),error=>error.code==='team_due_preview_expired');assert.equal(posts,0);
  store.disconnect();assert.equal(store.getSnapshot().operations.length,0);
});
test('Moscow input conversion validates leap days and never invents time or date',()=>{
  assert.equal(utcFromMoscow('2030-10-04T13:25'),'2030-10-04T10:25:00.000Z');
  for(const value of ['','2030-10-04','2030-02-29T13:00','0000-01-01T00:00','2030-10-04T25:00'])assert.equal(utcFromMoscow(value),undefined);
});
