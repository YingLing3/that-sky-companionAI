'use strict';
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert');
const root=path.resolve(__dirname,'..');
const desktop=fs.readFileSync(path.join(root,'app/web/desktop.js'),'utf8');
const start=desktop.indexOf('// RESOURCE_WORKFLOW_MODEL_BEGIN');
const end=desktop.indexOf('// RESOURCE_WORKFLOW_MODEL_END');
assert(start>=0&&end>start);
const model=vm.runInNewContext(desktop.slice(start,end)+'\nresourceStatus;',{});
const summary={account:'synthetic-current',catalog:{account:'synthetic-current',state:'checking',certified:false,message:'正在校验地图目录…'}};
let passed=0;
function check(fn){fn();passed++;}
check(()=>{const v=model.catalogView(summary,{});assert.equal(v.state,'running');assert(v.title.includes('正在校验'));assert.equal(v.progress.value,null);assert(!v.title.includes('成功'));});
check(()=>{const v=model.catalogView({...summary,catalog:{...summary.catalog,account:'synthetic-other'}},{});assert.notEqual(v.title,'正在校验地图目录…');assert.notEqual(v.state,'success');});
check(()=>{const v=model.catalogView(summary,{task:{kind:'catalog',running:true,msg:'building'}});assert(v.title.includes('构建'));});
check(()=>{const v=model.catalogView(summary,{task:{kind:'catalog',running:false,ok:false,error:'synthetic failure'}});assert.equal(v.state,'error');assert.equal(v.message,'synthetic failure');});
check(()=>{const v=model.catalogView(summary,{},'synthetic transport error');assert.equal(v.state,'error');});
check(()=>{const v=model.catalogView({...summary,catalog:{...summary.catalog,state:'ready',certified:true,maps:115,variants:1}},{});assert.equal(v.state,'success');assert.equal(v.progress.value,100);});
check(()=>{const v=model.catalogView({...summary,catalog:{...summary.catalog,state:'stale',code:'catalog_qianniaocheng_stale'}},{});assert.equal(v.state,'waiting');assert(v.message.includes('重新构建'));});
const wizard=fs.readFileSync(path.join(root,'app/web/wizard.js'),'utf8');
const ws=wizard.indexOf('function renderCatalogCertificate(summary)');
const we=wizard.indexOf('\n  function mountCleanup',ws);
assert(ws>=0&&we>ws);
function wizardRun(s){const nodes={};const $=id=>nodes[id]||(nodes[id]={value:0,textContent:''});vm.runInNewContext('('+wizard.slice(ws,we).trim()+')',{$})(s);return nodes;}
check(()=>{const n=wizardRun(summary);assert.equal(n['wz-cat-msg'].textContent,'正在校验地图目录…');assert.equal(n['wz-cat-pct'].textContent,'正在校验…');assert.equal(n['wz-cat-progress'].value,0);assert.equal(n['wz-catalog-start'].textContent,'构建地图目录');});
check(()=>{const n=wizardRun({...summary,catalog:{...summary.catalog,account:'synthetic-other',message:''}});assert.equal(n['wz-cat-pct'].textContent,'未生成 / 未通过校验');});
check(()=>{const n=wizardRun({...summary,catalog:{...summary.catalog,state:'ready',certified:true}});assert.equal(n['wz-cat-progress'].value,100);assert(n['wz-cat-pct'].textContent.includes('校验通过'));});
console.log(JSON.stringify({passed,failed:0}));
