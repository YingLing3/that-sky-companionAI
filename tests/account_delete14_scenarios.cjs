'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert/strict');
const root=path.resolve(__dirname,'..'),source=fs.readFileSync(path.join(root,'app/web/desktop.js'),'utf8');
function fn(name){
  const start=source.indexOf('function '+name+'(');assert(start>=0,name);
  const next=/\b(?:async )?function\s+[A-Za-z_$][\w$]*\s*\(/g;next.lastIndex=start+10;
  const m=next.exec(source);return source.slice(start,m?m.index:source.length).trim();
}
class Element{
  constructor(tag,cls,text){this.tag=tag;this.className=cls||'';this.textContent=text||'';this.childNodes=[];this.dataset={};this.listeners={};this.attrs={};this.hidden=false;this.disabled=false;this.value='';}
  appendChild(e){this.childNodes.push(e);return e;}
  setAttribute(k,v){this.attrs[k]=v;}
  addEventListener(k,v){this.listeners[k]=v;}
  focus(){}
}
const A='fixture-delete-a',B='fixture-keep-b';
function fixture(){
  const calls=[],confirms=[],toasts=[];
  const state={activeAccountId:A,accounts:[{id:A},{id:B}],status:{runner:false,online:false,chat:false,changing:false,going:null}};
  const ctx=vm.createContext({Map,Set,String,Array,Error,document:{},state,
    window:{confirm:(text)=>{confirms.push(text);return true;}},
    el:(...args)=>new Element(...args),isAccountActive:a=>a.id===state.activeAccountId,
    ApiError:Error,messageFrom:(raw,fallback)=>raw.message||fallback,friendlyError:e=>e.message,
    showToast:(...v)=>toasts.push(v),loadAccounts:async()=>{},refreshStatus:async()=>{},renderAccounts:()=>{},
    request:async(p,o)=>{calls.push([p,o]);return {ok:true,deleted:true,account:o.body.account};},
    postJson:async()=>({ok:true})});
  const declaration=source.match(/const accountOps=\{[^\n]+\};/)[0];
  vm.runInContext(declaration+'\n'+fn('accountOpButton')+'\n'+fn('appendAccountOperations'),ctx);
  function card(id=A){const card=new Element('article'),actions=new Element('div');ctx.appendAccountOperations(card,actions,{id,alias:id,nickname:'',serviceAlias:''});return {card,actions,remove:actions.childNodes.find(e=>e.textContent==='删除账号')};}
  return {ctx,state,calls,confirms,toasts,card};
}
let passed=0;
async function test(name,fn){await fn();passed++;}
(async()=>{
  await test('delete on every account',()=>{const f=fixture();assert(f.card(A).remove);assert(f.card(B).remove);});
  await test('active online disabled and cannot invoke delete',async()=>{const f=fixture();f.state.status.online=true;const c=f.card();assert.equal(c.remove.disabled,true);await c.remove.listeners.click();assert.equal(f.calls.length,0);assert.match(c.remove.title,/先让小光下线/);});
  await test('second confirmation cancelled',async()=>{const f=fixture();f.ctx.window.confirm=()=>false;await f.card().remove.listeners.click();assert.equal(f.calls.length,0);assert.equal(f.state.accounts.length,2);});
  await test('success exact selected id only',async()=>{const f=fixture();await f.card().remove.listeners.click();assert.equal(f.calls.length,1);assert.equal(f.calls[0][0],'/xiaoguang/accounts/delete');assert.deepEqual(JSON.parse(JSON.stringify(f.calls[0][1].body)),{account:A,confirm:true});assert.deepEqual(f.state.accounts.map(a=>a.id),[B]);assert.equal(f.state.activeAccountId,null);assert.match(f.confirms[0],/录制目录、人设和聊天记忆会保留/);assert.match(f.confirms[0],/不会删除小光服务账号或订阅/);});
  await test('inactive deletion does not clear active account',async()=>{const f=fixture();await f.card(B).remove.listeners.click();assert.deepEqual(f.state.accounts.map(a=>a.id),[A]);assert.equal(f.state.activeAccountId,A);});
  await test('timeout does not remove local row',async()=>{const f=fixture();f.ctx.request=async()=>{throw new Error('synthetic timeout');};await f.card().remove.listeners.click();assert.equal(f.state.accounts.length,2);assert.equal(f.state.activeAccountId,A);assert(f.toasts.some(v=>v[1]==='error'));});
  await test('wrong receipt never removes another account',async()=>{const f=fixture();f.ctx.request=async()=>({ok:true,deleted:true,account:B});await f.card(A).remove.listeners.click();assert.equal(f.state.accounts.length,2);assert.equal(f.state.activeAccountId,A);});
  await test('pending duplicate does not send twice',async()=>{const f=fixture();let release;f.ctx.request=(p,o)=>{f.calls.push([p,o]);return new Promise(resolve=>{release=resolve;});};const c=f.card();const first=c.remove.listeners.click();await c.remove.listeners.click();assert.equal(f.calls.length,1);release({ok:true,deleted:true,account:A});await first;assert.equal(f.state.accounts.length,1);});
  console.log(JSON.stringify({passed,failed:0}));
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
