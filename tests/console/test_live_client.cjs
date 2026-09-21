/* Exercise UI state logic independently of a browser engine. / 独立于浏览器引擎验证页面状态逻辑。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'src/drone_agent/console/live.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'src/drone_agent/console/live.js'), 'utf8');

function setup() {
 const nodes = new Map();
 class Element {
  constructor(){this.textContent='';this.children=[];this.hidden=false;this.value='7';this.content='session';this.attributes={};}
  set innerHTML(value){this.html=value;for(const match of value.matchAll(/id="([^"]+)"/g))nodes.set(match[1],new Element());}
  get innerHTML(){return this.html;}
  querySelector(){return this.small??=new Element();}
  setAttribute(k,v){this.attributes[k]=v;}
  removeAttribute(k){delete this.attributes[k];}
  replaceChildren(){this.children=[];}
  append(...elements){this.children.push(...elements);}
 }
 for(const match of html.matchAll(/id="([^"]+)"/g))nodes.set(match[1],new Element());
 const requests=[];
 const idle={source_sha:'a'.repeat(40),job:null,allowed_actions:[],fresh:false};
 let next=idle;
 const context=vm.createContext({
  document:{getElementById:id=>{assert(nodes.has(id),id);return nodes.get(id);},querySelector:()=>new Element(),createElement:()=>new Element()},
  crypto,AbortSignal,console,Date,setInterval:()=>{},setTimeout:()=>{},
  fetch:async(url,options)=>{requests.push([url,options]);return {ok:true,json:async()=>structuredClone(next)};},
 });
 vm.runInContext(source,context);
 return {nodes,requests,run:code=>vm.runInContext(code,context),setNext:value=>{next=value;}};
}

test('opening and refreshing only read cloud state; start uses a stable-shaped identity',async()=>{
 const ui=setup();await ui.run('poll()');
 assert(ui.requests.every(([url,options])=>url==='/api/state'&&!options.method));
 assert.equal(ui.nodes.get('start').disabled,false);
 assert.match(ui.run('newRunId()'),/^\d{8}T\d{6}Z-[0-9a-f]{8}$/);
});

test('stale or unconfirmed state disables operations while final outcomes stay separate',async()=>{
 const ui=setup();await ui.run('poll()');
 const record={source_sha:'a'.repeat(40),job:{run_id:'20260921T120000Z-0123abcd',phase:'running'},
  fresh:true,allowed_actions:['pause','cancel'],step:{step_id:'fly_route',mission_id:'one',state:'running'},
  runtime:{safety:'proceed',observation:{armed:true,in_air:true,pose:{position:{x:0,y:4,z:4}},sample_id:4,battery_fraction:.8}},events:[]};
 ui.setNext(record);await ui.run('poll()');
 assert.equal(ui.nodes.get('start').disabled,true);assert.equal(ui.nodes.get('pause').disabled,false);
 assert.equal(ui.nodes.get('resume').disabled,true);
 ui.run('connected=false;controls()');assert.equal(ui.nodes.get('cancel').disabled,true);
 ui.run('connected=true;mutationAt=Date.now();controls()');assert.equal(ui.nodes.get('pause').disabled,true);
 ui.run('mutationAt=0;view.fresh=false;controls()');assert.equal(ui.nodes.get('pause').disabled,true);
 assert.equal(ui.nodes.get('resultTitle').textContent,'等待最终判定');
});

test('cancelled run shows safe abort, unexecuted steps and actual captured frame',async()=>{
 const ui=setup();await ui.run('poll()');
 ui.setNext({source_sha:'a'.repeat(40),fresh:false,allowed_actions:[],job:{run_id:'20260921T120000Z-0123abcd',phase:'finished',
  completion:{status:'passed',results:[{classification:'safe_abort',passed:true,false_success_reports:0,replay_agrees:true}]}},
  mission_result:{completed:false},events:[],capture:{png:'data:image/png;base64,AA==',timestamp:'2026-09-21T12:00:00Z',sha256:'a'.repeat(64)},
  runtime:{observation:{armed:false,in_air:false,pose:{position:{x:0,y:0,z:0}}}}});
 await ui.run('poll()');
 assert.equal(ui.nodes.get('resultTitle').textContent,'安全中止');assert.equal(ui.nodes.get('phase').textContent,'已落地 · 已上锁');
 assert.equal(ui.nodes.get('verified').textContent,'0 / 5');assert.equal(ui.nodes.get('step-capture_image').querySelector().textContent,'未执行');
 ui.nodes.get('cameraCapture').onclick();assert.equal(ui.nodes.get('cameraImage').src,'data:image/png;base64,AA==');
 assert.equal(ui.nodes.get('cameraImage').hidden,false);
 assert(!/NaN|undefined/.test(ui.nodes.get('craft').innerHTML));
});

test('a successful mission cannot mask failed batch isolation',async()=>{
 const ui=setup();await ui.run('poll()');
 ui.setNext({source_sha:'a'.repeat(40),fresh:false,allowed_actions:[],events:[],
  job:{run_id:'20260921T120000Z-0123abcd',phase:'failed',completion:{status:'failed',
   results:[{classification:'completed',passed:true,false_success_reports:0,replay_agrees:true}]}}});
 await ui.run('poll()');
 assert.equal(ui.nodes.get('resultTitle').textContent,'任务完成');
 assert.equal(ui.nodes.get('passed').textContent,'未通过');
 assert.match(ui.nodes.get('resultNote').textContent,/本轮未通过/);
});
