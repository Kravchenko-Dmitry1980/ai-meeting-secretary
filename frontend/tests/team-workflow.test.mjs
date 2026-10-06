import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const OWNER = '00000000-0000-4000-8000-000000000001';
const MEMBER = '00000000-0000-4000-8000-000000000002';
const OPERATION = '00000000-0000-4000-8000-000000000003';
const NEXT_OPERATION = '00000000-0000-4000-8000-000000000004';
const NOW = '2026-10-04T21:00:00.000Z';
const actor = { id: OWNER, display_name: 'Дмитрий', role: 'owner', project_ids: ['7','8'], revision: 2 };
const member = { id: MEMBER, display_name: 'Алексей', revision: 3, project_ids: ['7'] };

async function load() {
  for (const file of ['model.ts','taskStore.ts']) {
    assert.ok(fs.existsSync(new URL(`../src/team/${file}`, import.meta.url)), `T9 ${file} absent`);
  }
  return { ...(await import('../src/team/model.ts')), ...(await import('../src/team/taskStore.ts')) };
}

function task(id='9007199254740993', changes={}) {
  return { task_id:id, project_id:'7', revision:4, remote_fingerprint:'a'.repeat(64),
    title:`Задача ${id}`, description:'', assignee_id:OWNER, bucket:'accepted',
    important:false, urgent:false, classification_confirmed:true, due_at:null,
    due_phrase:null, due_timezone:'Europe/Moscow', due_confirmed:true, origin:null,...changes };
}

function receipt(command,state='queued',changes={}) {
  return { acceptance_receipt:{operation_id:command.operation_id,payload_hash:'c'.repeat(64),
    decision:'accepted',decided_at:NOW,error_code:null},execution_state:{state,revision:1,
    task_id:command.task_id,verified_at:state==='applied'?NOW:null,error_code:null,retry_after:null},
    current:null,...changes };
}

function deferred() {
  let resolve,reject;
  const promise = new Promise((yes,no) => { resolve=yes;reject=no; });
  return {promise,resolve,reject};
}

function error(code,status,uncertain=false) { return Object.assign(new Error(code),{code,status,uncertain}); }

async function setup(changes={}) {
  const module = await load();
  const calls = [], listeners = new Set();
  const tasks = [task(),task('9007199254740995',{assignee_id:MEMBER})];
  const api = { actor, sessionEpoch:1, subscribeSession:(listener)=>{listeners.add(listener);return()=>listeners.delete(listener);},
    listTasks:async(projectId,options)=>{calls.push({kind:'list',projectId,options});return{items:tasks.filter(t=>t.project_id===projectId),next_cursor:null};},
    getTask:async(id)=>tasks.find(t=>t.task_id===id),
    postCommand:async(command)=>{calls.push({kind:'post',command});return receipt(command);},
    getReceipt:async(id)=>{calls.push({kind:'receipt',id});throw error('not_found',404);},
    ...changes };
  let sequence=0;
  const store = module.createTeamTaskStore(api,{clock:()=>Date.parse(NOW),uuid:()=>`00000000-0000-4000-8000-${String(++sequence+2).padStart(12,'0')}`});
  await store.selectProject('7');
  return {module,store,api,calls,tasks,listeners};
}

function draft(c,values={bucket:'doing'},id='9007199254740993') {
  c.store.setDraft({action:'set_state',taskId:id,values});
  return c.store.prepare();
}

test('canonical snapshots feed seven board buckets and independent matrix axes',async()=>{
  const {taskViews,BUCKETS} = await load();
  const tasks = [task(),task('50',{bucket:'doing',important:true,urgent:false}),
    task('51',{classification_confirmed:false,important:null,urgent:null}),
    task('52',{assignee_id:MEMBER})];
  const views=taskViews(tasks,{actorId:OWNER,filter:'mine',now:Date.parse(NOW)});
  assert.equal(BUCKETS.length,7);
  assert.equal(views.board.accepted[0],tasks[0]);
  assert.equal(views.matrix['not-important-not-urgent'][0],tasks[0]);
  assert.equal(views.matrix['important-not-urgent'][0],tasks[1]);
  assert.equal(views.matrix.unclassified[0],tasks[2]);
  assert.equal(views.tasks.length,3);
  assert.equal(taskViews(tasks,{actorId:OWNER,filter:'all',now:Date.parse(NOW)}).tasks.length,4);
});

test('terminal tasks remain in Kanban but never enter any matrix group',async()=>{
  const {taskViews} = await load();
  const tasks=[task('1',{important:true,urgent:true}),
    task('2',{bucket:'done',important:true,urgent:true}),
    task('3',{bucket:'cancelled',classification_confirmed:false,important:null,urgent:null}),
    task('4',{classification_confirmed:false,important:null,urgent:null})];
  const views=taskViews(tasks,{actorId:OWNER,filter:'all',now:Date.parse(NOW)});
  assert.equal(views.board.done[0],tasks[1]);
  assert.equal(views.board.cancelled[0],tasks[2]);
  assert.deepEqual(Object.values(views.matrix).flat().map(t=>t.task_id),['1','4']);
  assert.equal(views.matrix['important-urgent'][0],tasks[0]);
  assert.equal(views.matrix.unclassified[0],tasks[3]);
  assert.equal(views.tasks.length,4);
});

test('Today includes overdue at the Moscow boundary, closed tasks excluded, no date invented',async()=>{
  const {taskViews,dueLabel} = await load();
  const tasks=[task('1',{due_at:'2026-10-04T20:59:59.999Z'}),
    task('2',{due_at:NOW}),task('3',{due_at:'2026-10-05T20:59:59.999Z'}),
    task('4',{due_at:'2026-10-05T21:00:00Z'}),task('5',{due_confirmed:false}),
    task('6'),task('7',{bucket:'done',due_at:'2026-10-04T20:00:00Z'}),
    task('8',{bucket:'cancelled',due_at:'2026-10-04T20:00:00Z'})];
  const views=taskViews(tasks,{actorId:OWNER,filter:'all',now:Date.parse(NOW)});
  assert.deepEqual(views.today.map(t=>t.task_id),['1','2','3']);
  assert.deepEqual(views.overdue.map(t=>t.task_id),['1']);
  assert.deepEqual(views.noDue.map(t=>t.task_id),['5','6']);
  assert.equal(dueLabel(tasks[5]),'Без срока');
  assert.equal(dueLabel(tasks[4]),'Срок не подтверждён');
});

test('command builders preserve exact IDs/revisions and permit only action fields',async()=>{
  const {buildTaskCommand} = await load();
  const original=task();
  const command=buildTaskCommand({actor,projectId:'7',task:original,action:'classify',
    values:{important:true,urgent:false,classification_confirmed:true},operationId:OPERATION});
  assert.equal(command.task_id,'9007199254740993');
  assert.equal(command.expected_revision,4);
  assert.equal(command.expected_fingerprint,'a'.repeat(64));
  assert.deepEqual(command.values,{important:true,urgent:false,classification_confirmed:true});
  assert.ok(!('origin' in command));
  assert.ok(Object.isFrozen(command)&&Object.isFrozen(command.values));
  assert.throws(()=>buildTaskCommand({actor,projectId:'7',task:original,action:'classify',
    values:{bucket:'done',important:true,urgent:true,classification_confirmed:true},operationId:OPERATION}));
  assert.throws(()=>buildTaskCommand({actor,projectId:'999',task:original,action:'set_state',values:{bucket:'doing'},operationId:OPERATION}));
});

test('create/assign require explicit current assignee revision; clearing due is explicit',async()=>{
  const {buildTaskCommand,completionValues} = await load();
  const base={actor,projectId:'7',action:'create',values:{title:'Проверить отчёт',assignee_id:MEMBER},operationId:OPERATION};
  assert.throws(()=>buildTaskCommand(base));
  const create=buildTaskCommand({...base,assignee:member});
  assert.equal(create.expected_assignee_revision,3);
  assert.equal(create.task_id,null);
  const clear=buildTaskCommand({actor,projectId:'7',task:task(),action:'set_due',
    values:{due_at:null,due_confirmed:true,reason:'Срок снят владельцем'},operationId:OPERATION});
  assert.equal(clear.values.due_at,null);
  assert.ok(Object.hasOwn(clear.values,'due_at'));
  assert.deepEqual(completionValues(task(), 'Отчёт отправлен', actor),{bucket:'done',result:'Отчёт отправлен'});
  assert.deepEqual(completionValues(task('55',{important:true}), 'Проверено', actor),{bucket:'review',result:'Проверено'});
  assert.throws(()=>completionValues(task(),'  ',actor));
});

test('double confirm sends immutable operation once and queued never moves the board',async()=>{
  const pending=deferred();let submitted;
  const c=await setup({postCommand:(command)=>{submitted=command;return pending.promise;}});
  const prepared=draft(c);
  const first=c.store.confirm();const second=c.store.confirm();
  assert.equal(submitted,prepared);
  c.store.setDraft({values:{bucket:'blocked'}});
  pending.resolve(receipt(prepared));
  await Promise.all([first,second]);
  assert.equal(c.store.getSnapshot().operations.length,1);
  assert.equal(c.store.getSnapshot().operations[0].command.operation_id,OPERATION);
  assert.equal(c.store.getSnapshot().tasks[0].bucket,'accepted');
  assert.equal(c.store.getSnapshot().draft.values.bucket,'blocked');
  assert.equal(prepared.values.bucket,'doing');
});

test('successful HTTP with conflict merges current without replacing immutable captured payload',async()=>{
  const fresh=task(undefined,{revision:5,remote_fingerprint:'b'.repeat(64),bucket:'blocked'});
  const c=await setup({postCommand:async(command)=>receipt(command,'conflict',{
    acceptance_receipt:{...receipt(command).acceptance_receipt,decision:'rejected',error_code:'task_revision_conflict'},
    current:fresh})});
  const prepared=draft(c);
  await c.store.confirm();
  const state=c.store.getSnapshot();
  assert.equal(state.operations[0].receipt.execution_state.state,'conflict');
  assert.equal(state.tasks[0].revision,5);
  assert.equal(state.tasks[0].bucket,'blocked');
  assert.equal(prepared.expected_revision,4);
  assert.equal(prepared.values.bucket,'doing');
  assert.equal(c.module.taskViews(state.tasks,{actorId:OWNER,filter:'all',now:Date.parse(NOW)}).board.blocked[0],state.tasks[0]);
});

test('unknown POST reconciles same UUID via GET and never blindly resubmits on 404/503',async()=>{
  let posts=0;const reads=[];
  const c=await setup({postCommand:async()=>{posts++;throw error('network_unavailable',0,true);},
    getReceipt:async(id)=>{reads.push(id);throw error('not_found',404);}});
  const prepared=draft(c);
  await c.store.confirm();
  await c.store.confirm(OPERATION);
  c.api.getReceipt=async(id)=>{reads.push(id);throw error('gateway_unavailable',503);};
  await c.store.poll(OPERATION);
  assert.equal(posts,1);
  assert.ok(reads.length>=2&&reads.every(id=>id===OPERATION));
  assert.equal(c.store.getSnapshot().operations[0].transport,'unknown');
  assert.equal(c.store.getSnapshot().operations[0].command,prepared);
  assert.equal(c.store.getSnapshot().tasks[0].bucket,'accepted');
});

test('receipt progresses all seven states; older receipt/current cannot roll task backward',async()=>{
  const c=await setup();const prepared=draft(c);await c.store.confirm();
  let revision=1;
  for(const state of ['running','reconciling','uncertain','conflict','rejected','applied']) {
    c.api.getReceipt=async()=>receipt(prepared,state,{execution_state:{...receipt(prepared,state).execution_state,revision:++revision},
      current:task(undefined,{revision:6,bucket:'doing',remote_fingerprint:'c'.repeat(64)})});
    await c.store.poll(OPERATION);
    assert.equal(c.store.getSnapshot().operations[0].receipt.execution_state.state,state);
  }
  c.api.getReceipt=async()=>receipt(prepared,'queued',{current:task(undefined,{revision:5,bucket:'blocked'})});
  await c.store.poll(OPERATION);
  assert.equal(c.store.getSnapshot().operations[0].receipt.execution_state.state,'applied');
  assert.equal(c.store.getSnapshot().tasks[0].revision,6);
});

test('late project load/error cannot overwrite current scope or newer loading state',async()=>{
  const old=deferred();const current=deferred();
  const c=await setup();
  c.api.listTasks=(id)=>id==='7'?old.promise:current.promise;
  const requestA=c.store.refresh();const requestB=c.store.selectProject('8');
  old.reject(error('gateway_unavailable',503));await requestA;
  assert.equal(c.store.getSnapshot().projectId,'8');
  assert.equal(c.store.getSnapshot().loading,true);
  assert.equal(c.store.getSnapshot().errorCode,null);
  current.resolve({items:[task('80',{project_id:'8'})],next_cursor:null});await requestB;
  assert.deepEqual(c.store.getSnapshot().tasks.map(t=>t.task_id),['80']);
});

test('late task detail cannot overwrite selected task or draft after selection changes',async()=>{
  const first=deferred(),second=deferred();const c=await setup();
  c.api.getTask=(id)=>id==='9007199254740993'?first.promise:second.promise;
  const a=c.store.selectTask('9007199254740993');const b=c.store.selectTask('9007199254740995');
  c.store.setDraft({action:'comment',taskId:'9007199254740995',values:{comment:'Новый комментарий'}});
  first.resolve(task());await a;
  assert.equal(c.store.getSnapshot().selectedTaskId,'9007199254740995');
  second.resolve(task('9007199254740995'));await b;
  assert.equal(c.store.getSnapshot().selectedTask.task_id,'9007199254740995');
  assert.equal(c.store.getSnapshot().draft.values.comment,'Новый комментарий');
});

test('expired session clears scoped state, old-session receipts cannot change a new login',async()=>{
  const old=deferred();const c=await setup({postCommand:()=>old.promise});
  const prepared=draft(c);const post=c.store.confirm();
  c.api.actor=null;c.api.sessionEpoch++;for(const listener of c.listeners)listener();
  assert.equal(c.store.getSnapshot().actor,null);
  assert.deepEqual(c.store.getSnapshot().tasks,[]);
  assert.equal(c.store.getSnapshot().preview,null);
  c.api.actor={...actor,id:MEMBER};c.api.sessionEpoch++;for(const listener of c.listeners)listener();
  await c.store.selectProject('8');
  old.resolve(receipt(prepared,'applied',{current:task(undefined,{bucket:'doing',revision:5})}));await post;
  assert.equal(c.store.getSnapshot().actor.id,MEMBER);
  assert.equal(c.store.getSnapshot().projectId,'8');
  assert.deepEqual(c.store.getSnapshot().tasks,[]);
  assert.deepEqual(c.store.getSnapshot().operations,[]);
});

test('pagination uses exact string cursors and finds Today after an empty filtered first page',async()=>{
  const pages=[];
  const c=await setup({listTasks:async(_project,options)=>{pages.push(options.after??null);return options.after
    ?{items:[task('9007199254740995',{due_at:NOW})],next_cursor:null}
    :{items:[task('9007199254740993')],next_cursor:'9007199254740993'};}});
  assert.deepEqual(pages,[null,'9007199254740993']);
  const views=c.module.taskViews(c.store.getSnapshot().tasks,{actorId:OWNER,filter:'all',now:Date.parse(NOW)});
  assert.deepEqual(views.today.map(t=>t.task_id),['9007199254740995']);
});

test('mismatched receipt operation/project fails closed and does not leak task data',async()=>{
  const c=await setup({postCommand:async(command)=>receipt({...command,operation_id:NEXT_OPERATION},'applied',
    {current:task('foreign',{project_id:'999'})})});
  draft(c);await c.store.confirm();
  assert.equal(c.store.getSnapshot().tasks[0].bucket,'accepted');
  assert.equal(c.store.getSnapshot().operations[0].errorCode,'team_receipt_invalid');
  assert.ok(!c.store.getSnapshot().tasks.some(t=>t.project_id==='999'));
});

test('same actor login with a new session epoch fences in-flight state and receipts',async()=>{
  const pending=deferred();const c=await setup({postCommand:()=>pending.promise});
  const prepared=draft(c);const sent=c.store.confirm();
  c.api.actor={...actor};c.api.sessionEpoch++;for(const listener of c.listeners)listener();
  assert.equal(c.store.getSnapshot().projectId,null);
  assert.deepEqual(c.store.getSnapshot().tasks,[]);
  assert.deepEqual(c.store.getSnapshot().operations,[]);
  await c.store.selectProject('8');
  pending.resolve(receipt(prepared,'applied',{current:task(undefined,{revision:5,bucket:'doing'})}));await sent;
  assert.equal(c.store.getSnapshot().projectId,'8');
  assert.deepEqual(c.store.getSnapshot().operations,[]);
});

test('late metadata from an older refresh cannot replace the newer sync snapshot',async()=>{
  const c=await setup();const old=deferred();let requests=0;
  const metadata=(count)=>({project_id:'7',sync:{state:'ready',issue_count:count},cloud:{state:'not_configured'}});
  c.api.getStatus=()=>++requests===1?old.promise:Promise.resolve(metadata(2));
  const first=c.store.refresh();await c.store.refresh();
  assert.equal(c.store.getSnapshot().status.sync.issue_count,2);
  old.resolve(metadata(1));await first;
  assert.equal(c.store.getSnapshot().status.sync.issue_count,2);
});

test('metadata preserves exact money and member cursors, clears stale status after failure',async()=>{
  const c=await setup();const cursors=[];
  c.api.listMembers=async(_project,options)=>{cursors.push(options.after??null);return options.after
    ?{items:[{id:OWNER,display_name:'Дмитрий',revision:2}],next_cursor:null}
    :{items:[{id:MEMBER,display_name:'Алексей',revision:3}],next_cursor:MEMBER};};
  const status={project_id:'7',sync:{state:'not_configured',last_successful_sync_at:null,
    last_command_verified_at:NOW,issue_count:0},cloud:{state:'unavailable',remaining_rub:null,spend_rub:'0.000001'}};
  c.api.getStatus=async()=>status;await c.store.refresh();
  assert.deepEqual(cursors,[null,MEMBER]);
  assert.deepEqual(c.store.getSnapshot().members.map(m=>m.project_ids),[['7'],['7']]);
  assert.equal(c.store.getSnapshot().status.cloud.spend_rub,'0.000001');
  assert.equal(c.store.getSnapshot().status.cloud.remaining_rub,null);
  assert.equal(c.store.getSnapshot().status.sync.last_successful_sync_at,null);
  c.api.getStatus=async()=>{throw error('team_status_unavailable',503);};await c.store.refresh();
  assert.equal(c.store.getSnapshot().status,null);
  assert.equal(c.store.getSnapshot().metadataErrorCode,'team_status_unavailable');
  assert.ok(c.store.getSnapshot().tasks.length>0);
});

test('late history cannot overwrite a new selection or fabricate external actor/time',async()=>{
  const c=await setup();const oldHistory=deferred();const historyCalls=[];
  const entry={id:'external-1',kind:'external_change',event:'remote_changed',recorded_at:NOW,
    actor_id:null,actor_display_name:null,remote_occurred_at:null,changed_fields:['due_at'],
    values:{due_at:null,reason:'Срок снят',comment:'Точный текст'}};
  c.api.getHistory=async(id,options)=>{
    historyCalls.push([id,options.after??null]);
    if(id===c.tasks[0].task_id)return oldHistory.promise;
    return options.after?{items:[{...entry,id:'external-2'}],next_cursor:null}:{items:[entry],next_cursor:'opaque-cursor'};
  };
  const first=c.store.selectTask(c.tasks[0].task_id);await Promise.resolve();
  await c.store.selectTask(c.tasks[1].task_id);
  oldHistory.resolve({items:[{...entry,id:'old-task-entry'}],next_cursor:null});await first;
  const state=c.store.getSnapshot();
  assert.equal(state.selectedTaskId,c.tasks[1].task_id);
  assert.deepEqual(state.history.map(e=>e.id),['external-1','external-2']);
  assert.equal(state.history[0].actor_display_name,null);
  assert.equal(state.history[0].remote_occurred_at,null);
  assert.deepEqual(state.history[0].values,entry.values);
  assert.ok(Object.hasOwn(state.history[0].values,'due_at'));
  assert.equal(historyCalls.at(-1)[1],'opaque-cursor');
});

test('reversible effect connection survives cleanup and reconnect without disposing operations',async()=>{
  const c=await setup();const prepared=draft(c);await c.store.confirm();
  const disconnect=c.store.connect();disconnect();
  assert.equal(c.listeners.size,0);
  const secondDisconnect=c.store.connect();
  assert.equal(c.listeners.size,1);
  assert.equal(c.store.getSnapshot().operations[0].command,prepared);
  c.api.actor=null;c.api.sessionEpoch++;for(const listener of c.listeners)listener();
  assert.equal(c.store.getSnapshot().sessionExpired,true);
  assert.equal(c.store.getSnapshot().actor,null);
  secondDisconnect();assert.equal(c.listeners.size,0);
});

test('operation UUID collision never replaces an earlier immutable preview payload',async()=>{
  const c=await setup();
  const store=c.module.createTeamTaskStore(c.api,{uuid:()=>OPERATION,clock:()=>Date.parse(NOW)});
  await store.selectProject('7');store.setDraft({action:'set_state',taskId:c.tasks[0].task_id,values:{bucket:'doing'}});
  const first=store.prepare();store.setDraft({values:{bucket:'blocked'}});
  assert.throws(()=>store.prepare(),/team_operation_duplicate/);
  const receiptPromise=store.confirm(first.operation_id);
  await receiptPromise;
  assert.equal(store.getSnapshot().operations[0].command.values.bucket,'doing');
});

test('changing draft task and explicitly clearing create values cannot carry old text',async()=>{
  const c=await setup();
  c.store.setDraft({action:'comment',taskId:'9007199254740993',values:{comment:'Старый комментарий'}});
  c.store.setDraft({taskId:'9007199254740995'});
  assert.deepEqual(c.store.getSnapshot().draft.values,{});
  c.store.setDraft({action:'create',taskId:null,values:{title:'Старое название'},assignee:member});
  c.store.setDraft({action:'create',taskId:null,values:{},assignee:null});
  assert.deepEqual(c.store.getSnapshot().draft.values,{});
  assert.equal(c.store.getSnapshot().draft.assignee,null);
});

test('unknown old-project POST receipt cannot merge its current into newly selected project',async()=>{
  const pending=deferred();const c=await setup({postCommand:()=>pending.promise});
  const command=draft(c);const sent=c.store.confirm();
  await c.store.selectProject('8');
  c.api.getReceipt=async()=>receipt(command,'applied',{current:task(undefined,{revision:5,bucket:'doing'})});
  pending.reject(error('network_unavailable',0,true));await sent;
  assert.equal(c.store.getSnapshot().projectId,'8');
  assert.deepEqual(c.store.getSnapshot().tasks,[]);
  assert.deepEqual(c.store.getSnapshot().operations,[]);
});

test('accepted queued receipt clears only its unchanged draft and matching preview',async()=>{
  const c=await setup();draft(c);await c.store.confirm();
  assert.equal(c.store.getSnapshot().preview,null);
  assert.deepEqual(c.store.getSnapshot().draft,c.module.emptyDraft());
  assert.equal(c.store.getSnapshot().operations[0].receipt.execution_state.state,'queued');
  assert.equal(c.store.getSnapshot().tasks[0].bucket,'accepted');
});

test('older list cannot erase a task created by an applied receipt during refresh',async()=>{
  const oldList=deferred();const c=await setup();
  c.api.listTasks=()=>oldList.promise;
  const refresh=c.store.refresh();
  const created=task('70',{title:'Новая задача',revision:0,assignee_id:MEMBER,bucket:'inbox'});
  c.api.postCommand=async(command)=>receipt(command,'applied',{current:created});
  c.store.setDraft({action:'create',taskId:null,values:{title:'Новая задача',assignee_id:MEMBER},assignee:member});
  await c.store.confirm(c.store.prepare().operation_id);
  assert.ok(c.store.getSnapshot().tasks.some(t=>t.task_id==='70'));
  oldList.resolve({items:[c.tasks[0]],next_cursor:null});await refresh;
  assert.deepEqual(c.store.getSnapshot().tasks.map(t=>t.task_id),['70','9007199254740993']);
  assert.equal(c.store.getSnapshot().tasks.find(t=>t.task_id==='70').title,'Новая задача');
  assert.ok(!c.store.getSnapshot().tasks.some(t=>t.task_id===c.tasks[1].task_id));
});

test('older list revision cannot roll back an already known newer canonical snapshot',async()=>{
  const c=await setup();const command=draft(c);
  const newer=task(undefined,{revision:6,bucket:'doing',remote_fingerprint:'b'.repeat(64)});
  c.api.postCommand=async()=>receipt(command,'applied',{current:newer});await c.store.confirm();
  const canonical=c.store.getSnapshot().tasks.find(t=>t.task_id===command.task_id);
  c.api.listTasks=async()=>({items:[task(undefined,{revision:5,bucket:'accepted'})],next_cursor:null});
  await c.store.refresh();
  assert.equal(c.store.getSnapshot().tasks[0],canonical);
  assert.equal(c.store.getSnapshot().tasks[0].revision,6);
  assert.equal(c.store.getSnapshot().tasks[0].bucket,'doing');
});

test('conflict retains reviewable preview/draft; older accepted response preserves a newer preview',async()=>{
  const c=await setup({postCommand:async(command)=>receipt(command,'conflict')});
  const first=draft(c);await c.store.confirm();
  assert.equal(c.store.getSnapshot().preview,first);
  assert.equal(c.store.getSnapshot().draft.values.bucket,'doing');
  const pending=deferred();c.api.postCommand=()=>pending.promise;
  c.store.setDraft({values:{bucket:'blocked'}});const second=c.store.prepare();const sent=c.store.confirm(second.operation_id);
  c.store.setDraft({action:'comment',values:{comment:'Новый текст'}});
  const third=c.store.prepare();
  pending.resolve(receipt(second));await sent;
  assert.equal(c.store.getSnapshot().draft.values.comment,'Новый текст');
  assert.equal(c.store.getSnapshot().draft.action,'comment');
  assert.equal(c.store.getSnapshot().preview,third);
});

const settleHistory = () => new Promise(resolve => setImmediate(resolve));
function serverHistory(id, event='accepted', operation=null, changes={}) {
  return {id,kind:'command',event,recorded_at:NOW,operation_id:operation,
    actor_id:null,actor_display_name:null,action:'comment',execution_state:event==='accepted'?'queued':event,
    remote_occurred_at:null,changed_fields:[],values:{comment:'Точный серверный текст'},...changes};
}

test('open task history follows queued and applied receipts without reselecting the task',async()=>{
  const initial=serverHistory('initial');let rows=[initial];
  const c=await setup({getHistory:async()=>({items:rows,next_cursor:null})});
  await c.store.selectTask(c.tasks[0].task_id);
  const command=draft(c);rows=[serverHistory('accepted','accepted',command.operation_id),initial];
  await c.store.confirm();await settleHistory();
  assert.deepEqual(c.store.getSnapshot().history.map(row=>row.id),['accepted','initial']);
  rows=[serverHistory('applied','applied',command.operation_id),...rows];
  c.api.getReceipt=async()=>receipt(command,'applied',{execution_state:{...receipt(command,'applied').execution_state,revision:2},
    current:task(undefined,{revision:5,bucket:'doing'})});
  await c.store.poll(command.operation_id);await settleHistory();
  assert.equal(c.store.getSnapshot().selectedTask.bucket,'doing');
  assert.deepEqual(c.store.getSnapshot().history.map(row=>row.id),['applied','accepted','initial']);
});

test('comment receipt with null current refreshes authoritative history and preserves a newer draft and preview',async()=>{
  const pending=deferred();let rows=[];
  const c=await setup({postCommand:()=>pending.promise,getHistory:async()=>({items:rows,next_cursor:null})});
  await c.store.selectTask(c.tasks[0].task_id);
  c.store.setDraft({action:'comment',taskId:c.tasks[0].task_id,values:{comment:'Первый текст'}});
  const command=c.store.prepare();const sent=c.store.confirm();
  c.store.setDraft({action:'comment',values:{comment:'Следующий текст'}});const newer=c.store.prepare();
  const authoritative=serverHistory('server-comment','accepted',command.operation_id,{recorded_at:'2026-10-04T20:59:00.000Z'});
  rows=[authoritative];pending.resolve(receipt(command));
  await sent;await settleHistory();
  const state=c.store.getSnapshot();
  assert.deepEqual(state.history,[authoritative]);
  assert.equal(state.history[0].actor_display_name,null);
  assert.equal(state.history[0].remote_occurred_at,null);
  assert.equal(state.selectedTask.revision,4);
  assert.equal(state.draft.values.comment,'Следующий текст');assert.equal(state.preview,newer);
  const applied=serverHistory('server-comment-applied','applied',command.operation_id);
  rows=[applied,authoritative];c.api.getReceipt=async()=>receipt(command,'applied',{
    execution_state:{...receipt(command,'applied').execution_state,revision:2},current:c.tasks[0]});
  await c.store.poll(command.operation_id);await settleHistory();
  assert.deepEqual(c.store.getSnapshot().history,[applied,authoritative]);
  assert.equal(c.store.getSnapshot().selectedTask.revision,4);assert.equal(c.store.getSnapshot().preview,newer);
});

test('equal and older receipt revisions do not refresh or duplicate task history',async()=>{
  let calls=0;const entry=serverHistory('server-entry');
  const c=await setup({getHistory:async()=>{calls++;return{items:[entry],next_cursor:null};}});
  await c.store.selectTask(c.tasks[0].task_id);const command=draft(c);
  await c.store.confirm();await settleHistory();assert.equal(calls,2);
  c.api.getReceipt=async()=>receipt(command);await c.store.poll(command.operation_id);await settleHistory();
  assert.equal(calls,2);
  c.api.getReceipt=async()=>receipt(command,'queued',{execution_state:{...receipt(command).execution_state,revision:0}});
  await c.store.poll(command.operation_id);await settleHistory();assert.equal(calls,2);
  const applied=receipt(command,'applied',{execution_state:{...receipt(command,'applied').execution_state,revision:2},
    current:task(undefined,{revision:5,bucket:'doing',remote_fingerprint:'b'.repeat(64)})});
  c.api.getReceipt=async()=>applied;await c.store.poll(command.operation_id);await settleHistory();
  assert.equal(calls,3);await c.store.poll(command.operation_id);await settleHistory();assert.equal(calls,3);
  assert.deepEqual(c.store.getSnapshot().history,[entry]);
});

test('newest receipt history request wins and an older response cannot end its loading state',async()=>{
  const older=deferred(),newer=deferred();let calls=0;
  const c=await setup({getHistory:async()=>++calls===1?{items:[],next_cursor:null}:calls===2?older.promise:newer.promise});
  await c.store.selectTask(c.tasks[0].task_id);const command=draft(c);
  await c.store.confirm();
  c.api.getReceipt=async()=>receipt(command,'applied',{execution_state:{...receipt(command,'applied').execution_state,revision:2},
    current:task(undefined,{revision:5,bucket:'doing',remote_fingerprint:'b'.repeat(64)})});
  await c.store.poll(command.operation_id);
  assert.equal(c.store.getSnapshot().detailLoading,true);
  older.resolve({items:[serverHistory('stale')],next_cursor:null});await settleHistory();
  assert.equal(c.store.getSnapshot().detailLoading,true);
  assert.deepEqual(c.store.getSnapshot().history,[]);
  const current=serverHistory('current','applied',command.operation_id);
  newer.resolve({items:[current],next_cursor:null});await settleHistory();
  assert.deepEqual(c.store.getSnapshot().history,[current]);assert.equal(c.store.getSnapshot().detailLoading,false);
});

test('receipt history reload is fenced after closing or changing task, project, or session',async()=>{
  for(const change of ['close','task','project','session']) {
    const late=deferred();let calls=0;
    const c=await setup({getHistory:async(id)=>++calls===1?{items:[],next_cursor:null}:id===c.tasks[0].task_id?late.promise:
      {items:[serverHistory('other-task')],next_cursor:null}});
    await c.store.selectTask(c.tasks[0].task_id);draft(c);await c.store.confirm();
    if(change==='close')await c.store.selectTask(null);
    if(change==='task')await c.store.selectTask(c.tasks[1].task_id);
    if(change==='project')await c.store.selectProject('8');
    if(change==='session'){c.api.actor=null;c.api.sessionEpoch++;for(const listener of c.listeners)listener();}
    late.resolve({items:[serverHistory('late-wrong-scope')],next_cursor:null});await settleHistory();
    assert.deepEqual(c.store.getSnapshot().history.map(row=>row.id),change==='task'?['other-task']:[],change);
    assert.equal(c.store.getSnapshot().detailLoading,false,change);
  }
});

test('receipt history failures keep prior records and report failure without inventing a command event',async()=>{
  const prior=serverHistory('prior');let calls=0;
  const c=await setup({getHistory:async()=>{if(++calls===1)return{items:[prior],next_cursor:null};
    throw error('history_unavailable',503);}});
  await c.store.selectTask(c.tasks[0].task_id);const command=draft(c);await c.store.confirm();await settleHistory();
  const state=c.store.getSnapshot();assert.deepEqual(state.history,[prior]);
  assert.equal(state.errorCode,'history_unavailable');assert.equal(state.detailLoading,false);
  assert.equal(state.operations[0].receipt.execution_state.state,'queued');
  assert.ok(state.history.every(row=>row.operation_id!==command.operation_id));
});

test('a late history rejection cannot invalidate a newly selected task',async()=>{
  const late=deferred();let calls=0;
  void late.promise.catch(()=>{});
  const c=await setup({getHistory:async(id)=>++calls===1?{items:[],next_cursor:null}:id===c.tasks[0].task_id?late.promise:
    {items:[serverHistory('other-task')],next_cursor:null}});
  await c.store.selectTask(c.tasks[0].task_id);draft(c);await c.store.confirm();
  await c.store.selectTask(c.tasks[1].task_id);late.reject(error('history_unavailable',503));await settleHistory();
  assert.equal(c.store.getSnapshot().selectedTaskId,c.tasks[1].task_id);
  assert.deepEqual(c.store.getSnapshot().history.map(row=>row.id),['other-task']);
  assert.equal(c.store.getSnapshot().errorCode,null);assert.equal(c.store.getSnapshot().detailLoading,false);
});

test('receipt history reload reads every page and rejects a repeated cursor honestly',async()=>{
  const prior=serverHistory('prior');let mode='initial';const cursors=[];
  const c=await setup({getHistory:async(_id,options)=>{
    cursors.push(options.after??null);
    if(mode==='initial')return{items:[prior],next_cursor:null};
    if(!options.after)return{items:[serverHistory('first')],next_cursor:'next'};
    return{items:[serverHistory('second')],next_cursor:mode==='bad'?'next':null};
  }});
  await c.store.selectTask(c.tasks[0].task_id);const command=draft(c);mode='pages';
  await c.store.confirm();await settleHistory();
  assert.deepEqual(c.store.getSnapshot().history.map(row=>row.id),['first','second']);
  assert.deepEqual(cursors,[null,null,'next']);
  mode='bad';c.api.getReceipt=async()=>receipt(command,'applied',{execution_state:{...receipt(command,'applied').execution_state,revision:2},
    current:task(undefined,{revision:5,bucket:'doing',remote_fingerprint:'b'.repeat(64)})});
  await c.store.poll(command.operation_id);await settleHistory();
  assert.deepEqual(c.store.getSnapshot().history.map(row=>row.id),['first','second']);
  assert.equal(c.store.getSnapshot().errorCode,'team_pagination_invalid');assert.equal(c.store.getSnapshot().detailLoading,false);
});

test('receipt history supersedes a still pending initial selection history',async()=>{
  const initial=deferred();let calls=0;
  const current=serverHistory('current');
  const c=await setup({getHistory:async()=>++calls===1?initial.promise:{items:[current],next_cursor:null}});
  const selecting=c.store.selectTask(c.tasks[0].task_id);await Promise.resolve();
  draft(c);await c.store.confirm();await settleHistory();
  assert.deepEqual(c.store.getSnapshot().history,[current]);
  initial.resolve({items:[serverHistory('initial-stale')],next_cursor:null});await selecting;
  assert.deepEqual(c.store.getSnapshot().history,[current]);assert.equal(c.store.getSnapshot().detailLoading,false);
});

test('history pagination shows identical overlapping events once and refuses conflicting duplicate IDs',async()=>{
  const shared=serverHistory('shared'),second=serverHistory('second');let mode='initial';
  const c=await setup({getHistory:async(_id,options)=>mode==='initial'?{items:[],next_cursor:null}:
    !options.after?{items:[shared],next_cursor:'next'}:
    {items:[mode==='conflict'?{...shared,values:{comment:'Несогласованный текст'}}:shared,second],next_cursor:null}});
  await c.store.selectTask(c.tasks[0].task_id);const command=draft(c);mode='overlap';
  await c.store.confirm();await settleHistory();assert.deepEqual(c.store.getSnapshot().history,[shared,second]);
  mode='conflict';c.api.getReceipt=async()=>receipt(command,'applied',{execution_state:{...receipt(command,'applied').execution_state,revision:2},
    current:task(undefined,{revision:5,bucket:'doing',remote_fingerprint:'b'.repeat(64)})});
  await c.store.poll(command.operation_id);await settleHistory();
  assert.deepEqual(c.store.getSnapshot().history,[shared,second]);
  assert.equal(c.store.getSnapshot().errorCode,'team_history_invalid');assert.equal(c.store.getSnapshot().detailLoading,false);
});

test('late initial task rejection cannot replace successful receipt history with a stale error',async()=>{
  const initial=deferred();const authoritative=serverHistory('newest');
  const c=await setup({getTask:()=>initial.promise,getHistory:async()=>({items:[authoritative],next_cursor:null})});
  const selecting=c.store.selectTask(c.tasks[0].task_id);const command=draft(c);
  c.api.postCommand=async()=>receipt(command,'applied',{execution_state:{...receipt(command,'applied').execution_state,revision:2},
    current:task(undefined,{revision:5,bucket:'doing',remote_fingerprint:'b'.repeat(64)})});
  await c.store.confirm();await settleHistory();
  assert.deepEqual(c.store.getSnapshot().history,[authoritative]);
  initial.reject(error('old_task_read_failed',503));await selecting;
  assert.equal(c.store.getSnapshot().errorCode,null);
  assert.equal(c.store.getSnapshot().selectedTask.revision,5);
  assert.equal(c.store.getSnapshot().detailLoading,false);
});

test('same execution revision with newer selected task snapshot refreshes history once',async()=>{
  const prior=serverHistory('prior');let rows=[prior],calls=0;
  const c=await setup({getHistory:async()=>{calls++;return{items:rows,next_cursor:null};}});
  await c.store.selectTask(c.tasks[0].task_id);const command=draft(c);
  await c.store.confirm();await settleHistory();assert.equal(calls,2);
  const external=serverHistory('external','remote_changed',null,{kind:'external_change',action:null,
    execution_state:null,changed_fields:['bucket'],values:null});rows=[external,prior];
  const observed=receipt(command,'queued',{current:task(undefined,{revision:5,bucket:'blocked',remote_fingerprint:'b'.repeat(64)})});
  c.api.getReceipt=async()=>observed;
  await c.store.poll(command.operation_id);await settleHistory();
  assert.equal(c.store.getSnapshot().selectedTask.bucket,'blocked');
  assert.deepEqual(c.store.getSnapshot().history,[external,prior]);assert.equal(calls,3);
  await c.store.poll(command.operation_id);await settleHistory();assert.equal(calls,3);
  assert.equal(c.store.getSnapshot().operations[0].receipt.execution_state.revision,1);
});
