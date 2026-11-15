'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const ROOT=path.resolve(__dirname,'..'),results=[];
const old=fs.readFileSync(path.join(__dirname,'reset_cleanup_scenarios.cjs'),'utf8');
const fixture=vm.createContext({require,__dirname,console,process});
vm.runInContext(old.slice(0,old.indexOf('(async()=>{'))+'\nglobalThis.env=environment;',fixture);
const desktop=fs.readFileSync(path.join(ROOT,'app/web/desktop.js'),'utf8'),wizard=fs.readFileSync(path.join(ROOT,'app/web/wizard.js'),'utf8');
const plan=()=>({ok:true,plan_id:'synthetic-plan',expires_at:Date.now()/1000+300,total_bytes:100,
  selection:{terrain:true,catalog:true,names:true,captures:false,props:false},
  items:[{category:'terrain',path:'game/synthetic/terrain',bytes:100,files:3,action:'quarantine'}]});
function job(action,body,status='succeeded',result=undefined){return {id:'job-'+body.request_id,request_id:body.request_id,action,
  selection:body.selection||null,status,running:['queued','running'].includes(status),progress:{},
  result:result===undefined?(status==='succeeded'?(action.endsWith('preview')?plan():{ok:true,message:'操作已完成',selection:plan().selection}):null):result};}
const missing=()=>Object.assign(new Error('not found'),{status:404,data:{ok:false,error_code:'task_not_found',message:'任务记录不存在'}});
function storage(seed){const data=new Map(seed?[['xg.cleanup.active.v1',JSON.stringify(seed)]]:[]);return {data,getItem:k=>data.get(k)||null,setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k)};}
function env(handler,extra={}){
  const requests=[],delays=[],success=[],unknown=[];
  const e=fixture.env({rawProtocol:true,reconnect:false,delay:async ms=>{delays.push(ms);},
    request:async(url,body)=>{requests.push({url,body:body===undefined?undefined:JSON.parse(JSON.stringify(body))});return handler(url,body);},
    onSuccess:async(mode,result)=>success.push({mode,result}),onUnknown:async()=>unknown.push(true),...extra});
  return {...e,requests,delays,success,unknown};
}
const posts=e=>e.requests.filter(r=>r.body!==undefined), gets=e=>e.requests.filter(r=>r.body===undefined);
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
async function preview(e,mode='cleanup'){assert(e.ui.open(mode));assert(await e.ui.preview());}
async function test(name,fn){await fn();results.push(name);}
function normal(url,body){assert(body);const action=url.split('/').at(-1);return {ok:true,task:job(action,body)};}
(async()=>{
 await test('queued slow preview reports real counting and blocks duplicate confirmation',async()=>{
   let current,polls=0,release,waits=0;const gate=new Promise(r=>release=r);
   const e=env((url,body)=>{if(body){current=job('preview',body,'queued');return {ok:true,task:current};}
     polls++;return {ok:true,task:polls===1?{...current,status:'running',progress:{message:'统计真实目录',files_done:2,files_total:3,bytes_done:50,bytes_total:100}}:{...current,status:'succeeded',running:false,result:plan()}};},
     {delay:async()=>{if(++waits===2)await gate;}});
   e.ui.open('cleanup');const first=e.ui.preview();await flush();
   assert(e.ui.state.busy);assert.equal(e.ui.state.plan,null);assert(e.ui.nodes.status.textContent.includes('2/3'));
   assert(e.ui.nodes.status.textContent.includes('50 B/100 B'));assert.equal(await e.ui.preview(),false);
   e.ui.nodes.confirm.checked=true;assert.equal(await e.ui.commit(),false);assert.equal(e.ui.close(),false);
   assert(e.ui.nodes.fields.terrain.disabled);release();assert(await first);assert.equal(posts(e).length,1);
   assert.equal(e.ui.nodes.confirm.checked,false);assert.equal(e.ui.nodes.execute.disabled,true);
   assert.deepEqual([e.ui.nodes.feedback.textContent,e.ui.nodes.feedback.attributes.role],['预览已完成；尚未执行清理，请核对并确认','status']);
 });
 await test('preview completion is not a mutation and edited selection invalidates its plan',async()=>{
   const e=env(normal);await preview(e);assert.equal(e.success.length,0);
   e.ui.nodes.fields.props.checked=true;e.ui.nodes.fields.props.change();e.ui.nodes.confirm.checked=true;
   assert.equal(await e.ui.commit(),false);assert.equal(posts(e).length,1);
 });
 await test('slow mutation refreshes once only after final success',async()=>{
   let current,release;const gate=new Promise(r=>release=r);
   const e=env((url,body)=>{if(body&&url.endsWith('/preview'))return normal(url,body);
     if(body){current=job('execute',body,'running');return {ok:true,task:current};}
     return {ok:true,task:{...current,status:'succeeded',running:false,result:{ok:true,selection:plan().selection}}};},{delay:()=>gate});
   await preview(e);e.ui.nodes.confirm.checked=true;const done=e.ui.commit();await flush();
   assert.equal(e.success.length,0);assert.equal(await e.ui.commit(),false);assert(e.ui.state.busy);
   release();assert(await done);assert.equal(e.success.length,1);assert.equal(e.ui.state.open,false);
   assert.equal(posts(e).filter(r=>r.url.endsWith('/execute')).length,1);
 });
 await test('lost POST acknowledgement finds same request without resubmitting',async()=>{
   let current;
   const e=env((url,body)=>{if(body){current=job('preview',body);throw new Error('network timeout');}
     assert(url.includes('request_id='+encodeURIComponent(current.request_id)));assert(url.includes('action=preview'));return {ok:true,task:current};});
   await preview(e);assert.equal(posts(e).length,1);assert(gets(e).length>0);assert.equal(e.ui.state.busy,false);
 });
 await test('lost mutation ack never performs a second destructive action',async()=>{
   let current,effects=0;
   const e=env((url,body)=>{if(body&&url.endsWith('/preview'))return normal(url,body);
     if(body){effects++;current=job('execute',body);throw new Error('response lost');}return {ok:true,task:current};});
   await preview(e);e.ui.nodes.confirm.checked=true;assert(await e.ui.commit());
   assert.equal(effects,1);assert.equal(e.success.length,1);assert.equal(posts(e).length,2);
 });
 await test('not-found before delayed POST arrival does not release mutation guard',async()=>{
   let current,polls=0;
   const e=env((url,body)=>{if(body){current=job('preview',body);throw new Error('timeout');}
     if(++polls<3)throw missing();return {ok:true,task:current};});
   await preview(e);assert.equal(posts(e).length,1);assert.equal(gets(e).length,3);assert.equal(e.ui.state.busy,false);
 });
 await test('never-delivered submission explicitly recovers with identical identity and payload',async()=>{
   let sent=0;
   const e=env((url,body)=>{if(body){if(++sent===1)throw new Error('request never arrived');return normal(url,body);}throw missing();});
   e.ui.open('cleanup');assert.equal(await e.ui.preview(),false);assert(e.ui.state.busy&&e.ui.state.paused);
   assert.equal(gets(e).length,6);assert.equal(e.ui.nodes.resend.hidden,false);assert.equal(e.ui.nodes.resend.disabled,false);
   assert.equal(await e.ui.preview(),false);assert(await e.ui.resend());
   assert.deepEqual(posts(e)[0].body,posts(e)[1].body);assert.equal(e.ui.state.busy,false);assert(e.ui.state.plan);
 });
 await test('network-only outage pauses bounded retries and permits query only',async()=>{
   let current,online=false;
   const e=env((url,body)=>{if(body){current=job('preview',body);throw new Error('timeout');}
     if(!online)throw new Error('offline');return {ok:true,task:current};});
   e.ui.open('cleanup');assert.equal(await e.ui.preview(),false);assert(e.ui.state.busy&&e.ui.state.paused);
   assert.equal(gets(e).length,6);assert(e.delays.every(ms=>ms<=8000));assert.equal(e.ui.nodes.resend.hidden,true);
   assert.equal(e.ui.nodes.resume.disabled,false);assert.match(e.ui.nodes.status.textContent,/可能仍在处理/);
   assert.doesNotMatch(e.ui.nodes.status.textContent,/操作失败|请刷新后重试/);
   online=true;assert(await e.ui.resume());assert.equal(posts(e).length,1);
 });
 await test('failed task shows exact code and inert text rather than timeout fallback',async()=>{
   const e=env((url,body)=>({ok:true,task:job('preview',body,'failed',{ok:false,error_code:'scope_changed',message:'<img onerror=fixture>目录变化'})}));
   e.ui.open('cleanup');assert.equal(await e.ui.preview(),false);assert.equal(e.ui.state.busy,false);
   assert.match(e.ui.nodes.status.textContent,/<img onerror=fixture>目录变化.*scope_changed/);assert.equal(e.ui.nodes.status.attributes.role,'alert');
   assert.equal(e.success.length,0);
 });
 await test('wrong request identity never certifies another task result',async()=>{
   const bad=job('preview',{request_id:'wrong'});
   const e=env(()=>({ok:true,task:bad}));e.ui.open('cleanup');assert.equal(await e.ui.preview(),false);
   assert(e.ui.state.busy&&e.ui.state.paused);assert.equal(e.ui.state.plan,null);assert.equal(posts(e).length,1);
 });
 await test('busy response attaches authoritative existing job without repeating request',async()=>{
   const current=job('restore-preview',{request_id:'other'},'succeeded',plan());
   const e=env(()=>{throw Object.assign(new Error('busy'),{status:409,data:{ok:false,error_code:'cleanup_busy',task:current}});});
   e.ui.open('cleanup');assert(await e.ui.preview());assert.equal(e.ui.state.mode,'restore');assert.equal(posts(e).length,1);
 });
 await test('pre-submit definitive online rejection does not query or execute',async()=>{
   const e=env(()=>{throw Object.assign(new Error('online'),{status:409,data:{ok:false,error_code:'client_online',message:'先下线'}});});
   e.ui.open('cleanup');assert.equal(await e.ui.preview(),false);assert.equal(e.ui.state.busy,false);assert.equal(gets(e).length,0);
 });
 await test('reload reattaches confirmed mutation and refreshes without POST',async()=>{
   const saved={request_id:'saved',task_id:'job-saved',action:'execute',mode:'cleanup',selection:plan().selection};
   const local=storage(saved),current=job('execute',{request_id:'saved'});
   const e=env((url,body)=>{assert.equal(body,undefined);assert(url.includes('task_id=job-saved'));return {ok:true,task:current};},{storage:local});
   assert(await e.ui.reconnect());assert.equal(e.success.length,1);assert.equal(posts(e).length,0);assert.equal(local.data.size,0);
 });
 await test('reload preview still requires a new explicit confirmation',async()=>{
   const current=job('preview',{request_id:'saved',selection:{...plan().selection,props:true}});
   const e=env(()=>({ok:true,task:current}),{storage:storage({request_id:'saved',task_id:current.id,action:'preview'})});
   assert(await e.ui.reconnect());assert(e.ui.state.plan);assert.equal(e.ui.nodes.confirm.checked,false);
   assert.equal(await e.ui.commit(),false);assert.equal(posts(e).length,0);assert(e.ui.nodes.fields.props.checked);
 });
 await test('initial reattach lookup immediately explains why actions are disabled',async()=>{
   let release;const gate=new Promise(r=>release=r);const e=env(async()=>{await gate;return {ok:true,task:null};});
   const pending=e.ui.reconnect();assert(e.ui.state.busy);assert.match(e.ui.nodes.feedback.textContent,/正在查询.*后台维护任务/);
   release();assert.equal(await pending,false);assert.equal(e.ui.state.busy,false);assert.equal(posts(e).length,0);
 });
 await test('page discovery attaches running backend task even without local storage',async()=>{
   let polls=0;const current=job('purge',{request_id:'current'},'running');
   const e=env(()=>({ok:true,task:++polls===1?current:{...current,status:'succeeded',running:false,result:{ok:true}}}));
   assert(await e.ui.reconnect());assert.equal(posts(e).length,0);assert.equal(e.success[0].mode,'purge');
 });
 await test('expired confirmed record plus empty current refreshes unknown outcome and unlocks new preview',async()=>{
   const local=storage({request_id:'expired',task_id:'job-expired',action:'execute'});
   const e=env(url=>{if(url.includes('?'))throw missing();return {ok:true,task:null};},{storage:local});
   assert.equal(await e.ui.reconnect(),false);assert.equal(e.ui.state.busy,false);assert.equal(e.ui.state.plan,null);
   assert.equal(e.unknown.length,1);assert.equal(e.success.length,0);assert.equal(posts(e).length,0);
   assert.match(e.ui.nodes.feedback.textContent,/原任务结果未知.*未重放/);assert.equal(local.data.size,0);
 });
 await test('expired old record attaches current other job instead of enabling concurrent cleanup',async()=>{
   const current=job('preview',{request_id:'new'});
   const e=env(url=>{if(url.includes('job-old'))throw missing();return {ok:true,task:current};},
     {storage:storage({request_id:'old',task_id:'job-old',action:'execute'})});
   assert(await e.ui.reconnect());assert.equal(e.unknown.length,0);assert.equal(posts(e).length,0);assert(e.ui.state.plan);
 });
 await test('expired record does not unlock when current-job query itself is unavailable',async()=>{
   const e=env(url=>{if(url.includes('?'))throw missing();throw new Error('current unavailable');},
     {storage:storage({request_id:'old',task_id:'job-old',action:'execute'})});
   assert.equal(await e.ui.reconnect(),false);assert(e.ui.state.busy&&e.ui.state.paused);assert.equal(e.unknown.length,0);
 });
 await test('restore and purge both use background preview plus explicit confirm task',async()=>{
   for(const mode of ['restore','purge']){const e=env(normal);await preview(e,mode);assert.equal(await e.ui.commit(),false);
     e.ui.nodes.confirm.checked=true;assert(await e.ui.commit());assert.equal(posts(e).length,2);
     assert(posts(e)[0].url.endsWith(mode+'-preview'));assert(posts(e)[1].url.endsWith(mode));assert(posts(e)[1].body.confirm);}
 });
 await test('onBusy covers pending refresh and success occurs once',async()=>{
   let release,called=0;const gate=new Promise(r=>release=r);
   const e=env(normal,{onSuccess:async()=>{called++;await gate;}});await preview(e);e.ui.nodes.confirm.checked=true;
   const done=e.ui.commit();await flush();assert(e.ui.state.busy);assert.equal(called,1);release();assert(await done);assert.equal(e.ui.state.busy,false);
 });
 await test('progress strings are rendered as inert text',async()=>{
   let current,release;const gate=new Promise(r=>release=r);
   const e=env((url,body)=>{if(body){current=job('preview',body,'running');current.progress={message:'<script>fixture</script>',files_done:3};return {ok:true,task:current};}
      return {ok:true,task:{...current,status:'succeeded',running:false,result:plan()}};},{delay:()=>gate});
   e.ui.open('cleanup');const pending=e.ui.preview();await flush();assert.match(e.ui.nodes.status.textContent,/<script>fixture<\/script>/);release();assert(await pending);
 });
 await test('failed partial mutation refreshes resources but retains actual failure',async()=>{
   const e=env((url,body)=>url.endsWith('/preview')?normal(url,body):({ok:true,task:job('execute',body,'failed',
     {ok:false,error_code:'rollback_incomplete',message:'已有部分目录移动，回滚未完整完成'})}));
   await preview(e);e.ui.nodes.confirm.checked=true;assert.equal(await e.ui.commit(),false);
   assert.equal(e.unknown.length,1);assert.equal(e.success.length,0);assert.equal(e.ui.state.busy,false);
   assert.match(e.ui.nodes.feedback.textContent,/rollback_incomplete/);assert.equal(e.ui.nodes.feedback.attributes.role,'alert');
 });
 await test('partial failure refresh outage cannot replace original error or claim success',async()=>{
   const e=env((url,body)=>url.endsWith('preview')?normal(url,body):({ok:true,task:job('restore',body,'failed',
     {ok:false,error_code:'partial_restore',message:'部分恢复失败'})}),{onUnknown:async()=>{throw new Error('refresh timeout');}});
   await preview(e,'restore');e.ui.nodes.confirm.checked=true;assert.equal(await e.ui.commit(),false);
   assert.match(e.ui.nodes.feedback.textContent,/partial_restore.*资源状态刷新未完成/);assert.equal(e.success.length,0);
 });
 await test('never-delivered confirmed mutation resends exactly original plan and request identity',async()=>{
   let submits=0,effects=0;
   const e=env((url,body)=>{if(body&&url.endsWith('/preview'))return normal(url,body);
     if(body){if(++submits===1)throw new Error('never delivered');effects++;return normal(url,body);}throw missing();});
   await preview(e);e.ui.nodes.confirm.checked=true;assert.equal(await e.ui.commit(),false);assert(e.ui.state.busy);
   assert(await e.ui.resend());const mutation=posts(e).filter(r=>r.url.endsWith('/execute'));
   assert.deepEqual(mutation[0].body,mutation[1].body);assert.equal(mutation[1].body.plan_id,'synthetic-plan');
   assert.equal(mutation[1].body.confirm,true);assert.equal(effects,1);assert.equal(e.success.length,1);
 });
 await test('desktop uses finite 15-second authenticated request for GET and POST',async()=>{
   const calls=[],ctx=vm.createContext({request:async(p,o)=>{calls.push({p,o});return {ok:true};},ApiError:Error,messageFrom:()=>''});
   const line=desktop.split('\n').find(x=>x.startsWith('async function parityApi('));vm.runInContext(line+'\nglobalThis.api=parityApi;',ctx);
   await ctx.api('/api/wizard/cleanup/task?task_id=x');await ctx.api('/api/wizard/cleanup/execute',{request_id:'x'});
   assert.equal(calls[0].o.timeoutMs,15000);assert.equal(calls[0].o.method,undefined);assert.equal(calls[1].o.method,'POST');assert.equal(calls[1].o.timeoutMs,15000);
 });
 await test('wizard adapter uses GET for polling and finite timeout without losing cookie API',()=>{
   let options;const ctx=vm.createContext({window:{XGDataCleanup:{mount:(_h,o)=>options=o}},state:{},$:()=>({}),api:(p,o)=>({p,o})});
   const a=wizard.indexOf('  function mountCleanup()'),b=wizard.indexOf('  function renderSummary()',a);
   vm.runInContext(wizard.slice(a,b)+'\nmountCleanup();',ctx);const get=options.request('/api/wizard/cleanup/task'),post=options.request('/api/wizard/cleanup/preview',{});
   assert.equal(get.o.method,undefined);assert.equal(get.o.timeoutMs,15000);assert.equal(post.o.method,'POST');assert.equal(post.o.timeoutMs,15000);
 });
 await test('wizard finite request timeout aborts only the HTTP wait',async()=>{
   const match=wizard.match(/^  async function api\([^]*?^  }/m);assert(match);let timer,cleared=false,init;
   const ctx=vm.createContext({AbortController,setTimeout:(fn,ms)=>{assert.equal(ms,15000);timer=fn;return 1;},clearTimeout:()=>{cleared=true;},
     fetch:(_p,o)=>{init=o;return new Promise((_resolve,reject)=>o.signal.addEventListener('abort',()=>reject(new Error('aborted'))));}});
   vm.runInContext(match[0]+'\nglobalThis.run=api;',ctx);const pending=ctx.run('/api/wizard/cleanup/task',{timeoutMs:15000});
   assert.equal(init.credentials,'same-origin');assert.equal(init.timeoutMs,undefined);timer();await assert.rejects(pending,/aborted/);assert(cleared);
 });
 console.log(JSON.stringify({passed:results.length,failed:0,tests:results},null,2));
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
