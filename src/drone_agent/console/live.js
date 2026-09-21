"use strict";
const byId=id=>document.getElementById(id);
const names={takeoff:"起飞",fly_route:"巡检",capture_image:"拍照",return_home:"返航",land:"降落"};
const lifecycle={preparing:"准备中",running:"执行中",verifying:"确认效果",completed:"已完成",pause_requested:"暂停中",paused:"已暂停",cancel_requested:"取消中",recovering:"安全收尾",cancelled:"已取消",outcome_unknown:"结果未知",failed:"失败"};
const phaseNames={preparing:"准备环境",running:"执行中",judging:"安全收尾 / 裁判",finished:"本次已结束",failed:"本次未通过",interrupted:"后台状态待核对"};
let view=null,connected=false,busy=false,lastResponse=0,photoMode="live",exportRun=null,lastEventKey="",lastJob=null,mutationAt=0;
const nonce=document.querySelector('meta[name="console-nonce"]').content;
const project=p=>[350+(p[0]-p[1])*43,375-(p[0]+p[1])*20-p[2]*42];
const xy=p=>project(p).map(v=>v.toFixed(2)).join(",");
const valid=p=>Array.isArray(p)&&p.length===3&&p.every(Number.isFinite);
const seg=(a,b,color,extra="")=>`<line x1="${project(a)[0]}" y1="${project(a)[1]}" x2="${project(b)[0]}" y2="${project(b)[1]}" stroke="${color}" ${extra}/>`;
function notice(message){byId("notice").textContent=message;byId("notice").hidden=!message;}
function newRunId(){return new Date().toISOString().replace(/[-:]/g,"").slice(0,15)+"Z-"+crypto.randomUUID().replace(/-/g,"").slice(0,8);}
async function api(path,body){
 const options={cache:"no-store",signal:AbortSignal.timeout(30000)};
 if(body){options.method="POST";options.headers={"Content-Type":"application/json","X-Console-Nonce":nonce};options.body=JSON.stringify(body);}
 const response=await fetch(path,options);const value=await response.json();
 if(!response.ok)throw new Error(value.error||"请求未确认");return value;
}
function controls(){
 const job=view?.job,active=!!job&&!['finished','failed'].includes(job.phase);
 const linkOK=connected&&Date.now()-lastResponse<4000;
 byId("start").disabled=busy||mutationAt>0||!linkOK||active;
 byId("seed").disabled=busy||active;
 for(const action of ["pause","resume","cancel"])byId(action).disabled=busy||mutationAt>0||!linkOK||!view?.fresh||!(view.allowed_actions||[]).includes(action);
 byId("evidence").disabled=busy||!linkOK||!job||!['finished','failed'].includes(job.phase)||!job.completion?.results||exportRun===job.run_id;
 byId("connection").className="connection"+(linkOK?" good":"");
 byId("connectionText").textContent=linkOK?"已连接云端":"连接中断 / 等待重连";
 if(!linkOK&&view)byId("viewStatus").textContent="连接中断 · 历史快照";
}
function drawMap(){
 const scene=view?.scene;
 let ground="";
 for(let i=-2;i<=7;i++)ground+=seg([i,-2,0],[i,7,0],"#35523f",'stroke-width=".65"')+seg([-2,i,0],[7,i,0],"#35523f",'stroke-width=".65"');
 if(scene){
  const asset=scene.assets?.asset_red?.position;
  if(valid(asset)){
   const square=[[-1,-1],[1,-1],[1,1],[-1,1]].map(p=>xy([asset[0]+p[0],asset[1]+p[1],asset[2]]));
   ground+=`<polygon points="${square.join(' ')}" fill="#a65446" stroke="#dc886f"/>`;
   ground+=`<text x="${project(asset)[0]}" y="${project(asset)[1]+29}" text-anchor="middle">ASSET_RED</text>`;
  }
  const path=[[0,0,0],[0,0,4],...(scene.routes?.inspection||[]),...(scene.routes?.return||[]),[0,0,0]].filter(valid);
  ground+=`<polyline points="${path.map(xy).join(' ')}" stroke="#8ca27c" stroke-width="1" stroke-dasharray="5 6" fill="none"/>`;
 }
 ground+='<ellipse cx="350" cy="375" rx="35" ry="17" fill="#324c36" stroke="#9ab17b"/><text x="350" y="380" text-anchor="middle" style="fill:#d4ed95;font-size:15px">H</text><text x="350" y="413" text-anchor="middle">HOME / 0, 0</text>';
 ground+=seg([-2,-2,0],[0,-2,0],"#73896d")+seg([-2,-2,0],[-2,0,0],"#73896d");
 byId("ground").innerHTML=ground;
 const points=(view?.estimate||[]).map(r=>r.position).filter(valid);
 byId("track").innerHTML=`<polyline points="${points.map(xy).join(' ')}" fill="none" stroke="#d4ed95" stroke-width="2.5" stroke-linejoin="round"/>`;
 const p=view?.runtime?.observation?.pose?.position,position=p?[p.x,p.y,p.z]:null;
 if(!valid(position)){byId("craft").innerHTML="";return;}
 const pos=project(position),shadow=project([position[0],position[1],0]);
 byId("craft").innerHTML=`<ellipse cx="${shadow[0]}" cy="${shadow[1]}" rx="13" ry="6" fill="#080f0a" opacity=".5"/>${seg(position,[position[0],position[1],0],"#91a974",'stroke-dasharray="3 5" opacity=".6"')}<g transform="translate(${pos[0]},${pos[1]})"><path d="M-12,-7 12,7 M-12,7 12,-7" stroke="#e6efdb" stroke-width="3"/><g fill="none" stroke="#e6efdb" stroke-width="1.4"><ellipse cx="-13" cy="-7" rx="8" ry="4"/><ellipse cx="13" cy="7" rx="8" ry="4"/><ellipse cx="-13" cy="7" rx="8" ry="4"/><ellipse cx="13" cy="-7" rx="8" ry="4"/></g><rect x="-5" y="-5" width="10" height="10" rx="3" fill="${view.fresh?'#d4ed95':'#c99464'}"/></g>`;
}
function render(){
 const job=view.job,obs=view.runtime?.observation,events=view.events||[],result=job?.completion?.results?.[0];
 if(job?.run_id!==lastJob){lastJob=job?.run_id;photoMode="live";exportRun=null;byId("evidenceLink").hidden=true;byId("evidence").textContent="拉取并核对完整证据";byId("operatorResult").textContent="";lastEventKey="";}
 const terminal=job&&['finished','failed'].includes(job.phase);
 byId("version").textContent="运行版本 "+(job?.source_sha||view.source_sha).slice(0,7);
 byId("runId").textContent=job?"RUN · "+job.run_id:"RUN · 尚未开始";
 byId("viewStatus").textContent=!job?"等待任务":terminal?"本次归档快照":view.fresh?"实时观测":"等待新鲜观测";
 let phase=job?phaseNames[job.phase]:"等待开始";
 if(job?.phase==='running'&&view.step)phase=(names[view.step.step_id]||view.step.step_id)+" · "+(lifecycle[view.step.state]||view.step.state);
 if(view.runtime?.safety==='recover')phase="安全恢复中";
 if(terminal&&obs?.armed===false&&obs?.in_air===false)phase="已落地 · 已上锁";
 byId("phase").textContent=phase;
 byId("altitude").textContent=Number.isFinite(obs?.pose?.position?.z)?obs.pose.position.z.toFixed(2):"—";
 byId("battery").textContent=Number.isFinite(obs?.battery_fraction)?Math.round(obs.battery_fraction*100):"—";
 byId("mode").textContent="MODE "+(obs?.flight_mode||"—");
 byId("armed").textContent="解锁 "+(obs?.armed===true?"是":obs?.armed===false?"否":"—");
 byId("airborne").textContent="在空 "+(obs?.in_air===true?"是":obs?.in_air===false?"否":"—");
 byId("sample").textContent="SAMPLE "+(obs?.sample_id??"—");
 let count=0;
 for(const id of Object.keys(names)){
  const recent=events.filter(e=>e.kind==='skill_state'&&e.data.step_id===id).at(-1);
  const outcome=events.filter(e=>e.kind==='step_outcome'&&e.data.outcome.step_id===id).at(-1)?.data.outcome;
  const verified=outcome?.execution_status==='succeeded'&&outcome?.effect_verdict==='verified';
  if(verified)count++;
  const node=byId("step-"+id);node.className="step "+(verified?'done':outcome?'unknown':recent?'active':'');
  node.querySelector('small').textContent=verified?'已证实':outcome?'未完成':recent?(lifecycle[recent.data.state]||recent.data.state):view.mission_result?'未执行':'待执行';
 }
 byId("verified").textContent=count+" / 5";
 const classification=result?.classification;
 byId("resultTitle").textContent=classification?({completed:'任务完成',safe_abort:'安全中止',unsafe_or_incorrect:'存在异常'}[classification]||classification):job?(job.phase==='failed'?'运行失败':job.phase==='interrupted'?'状态待核对':'等待最终判定'):'等待任务';
 byId("resultTitle").className="result-title"+(classification==='safe_abort'||(result&&!result.passed)||job?.phase==='failed'?' warn':'');
 byId("falseSuccess").textContent=result?.false_success_reports??"—";
 byId("replay").textContent=result?.replay_agrees===true?'与在线一致':result?.replay_agrees===false?'不一致':'等待结果';
 const batchPassed=job?.completion?.status==='passed'&&result?.passed===true;
 byId("passed").textContent=job?.completion?(batchPassed?'通过':'未通过'):'等待裁判';
 byId("resultNote").textContent=job?.completion?(batchPassed?'结果来自独立裁判；完整证据可继续拉取核对。':'本轮未通过，请查看完整证据；任务效果与环境隔离验收分开核对。'):'收到命令不等于任务完成；最终结果由飞行证据与独立裁判确认。';
 const image=photoMode==='capture'?view.capture:view.camera;
 byId("cameraCapture").disabled=!view.capture;byId("cameraLive").setAttribute('aria-pressed',String(photoMode==='live'));byId("cameraCapture").setAttribute('aria-pressed',String(photoMode==='capture'));
 byId("cameraImage").hidden=!image;byId("cameraEmpty").hidden=!!image;
 if(image){byId("cameraImage").src=image.png;byId("cameraMeta").textContent=(photoMode==='capture'?'已保存证据 · ':'采样 · ')+new Date(image.timestamp).toLocaleTimeString('zh-CN')+(image.sha256?' · '+image.sha256.slice(0,12):terminal?' · 归档帧':'');}
 else {byId("cameraImage").removeAttribute('src');byId("cameraMeta").textContent='cam_0 · 等待采样';}
 const operation=view.operation;
 if(operation)byId("operatorResult").textContent=({pause:'暂停',resume:'恢复',cancel:'取消'}[operation.action]||operation.action)+"请求："+({pending:'已提交，等待机载确认',accepted:'机载已接受',rejected:'已拒绝，请刷新状态后操作'}[operation.status]||operation.status)+(operation.reason?'（'+operation.reason+'）':'');
 const significant=events.filter(e=>!['lease','intent','receipt','command_reconciled'].includes(e.kind));
 const key=job?.run_id+':'+significant.length;
 if(key!==lastEventKey){
  const list=byId('events');list.replaceChildren();
  for(const e of significant.slice(-60).reverse()){
   const item=document.createElement('div');item.className='event'+(/intervention|rejected|error/.test(e.kind)?' alert':'');
   const stamp=document.createElement('time');stamp.textContent=new Date(e.timestamp).toLocaleTimeString('zh-CN');
   const label=document.createElement('span');label.textContent=e.label||e.kind;
   item.append(stamp,label);list.append(item);
  }
  if(!significant.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='等待机载任务事件。';list.append(empty);}
  lastEventKey=key;
 }
 if(job?.phase==='interrupted')notice('云端后台进程状态异常，需要核对当前运行；本页不会自动重新启动任务。');
 else if(job?.phase==='failed'&&job.completion?.error)notice('本次运行失败：'+job.completion.error);
 drawMap();controls();
}
async function submit(path,body){
 busy=true;controls();notice('');
 try{await api(path,body);byId('operatorResult').textContent='请求已提交，等待云端状态确认。';}
 catch(error){notice('请求未确认：'+error.message+'。请先核对当前状态，本页没有自动重发。');}
 finally{busy=false;mutationAt=Date.now();controls();}
}
byId('start').onclick=()=>submit('/api/start',{run_id:newRunId(),seed:Number(byId('seed').value)});
for(const action of ['pause','resume','cancel'])byId(action).onclick=()=>{
 if(!view?.job||!view.step)return;
 submit('/api/operate',{run_id:view.job.run_id,request_id:crypto.randomUUID().replace(/-/g,''),operation:action,mission_id:view.step.mission_id,step_id:view.step.step_id});
};
byId('cameraLive').onclick=()=>{photoMode='live';if(view)render();};
byId('cameraCapture').onclick=()=>{photoMode='capture';if(view)render();};
async function updateExport(){
 if(!exportRun)return;
 const exported=await api('/api/evidence?run='+encodeURIComponent(exportRun));
 if(exported.status==='ready'){byId('evidence').textContent='证据已核对';byId('evidenceLink').href=exported.url;byId('evidenceLink').hidden=false;}
 if(exported.status==='failed'){notice('证据拉取失败：'+exported.reason);byId('evidence').textContent='重新拉取完整证据';exportRun=null;controls();}
}
byId('evidence').onclick=async()=>{
 if(!view?.job)return;
 exportRun=view.job.run_id;byId('evidence').textContent='正在拉取与核对…';controls();
 try{await api('/api/evidence',{run_id:exportRun});await updateExport();}
 catch(error){notice('证据拉取未确认：'+error.message);exportRun=null;controls();}
};
byId('steps').innerHTML=Object.keys(names).map((id,i)=>`<div class="step" id="step-${id}"><i>${i+1}</i>${names[id]}<small>待执行</small></div>`).join('');
drawMap();
async function poll(){
 const begin=Date.now();
 try{view=await api('/api/state');connected=true;lastResponse=Date.now();if(begin>=mutationAt)mutationAt=0;if(byId('notice').textContent.startsWith('暂时无法获取云端状态'))notice('');byId('latency').textContent=(lastResponse-begin)+' ms';render();await updateExport();}
 catch(error){connected=false;notice('暂时无法获取云端状态：'+error.message+'。正在重新连接；云端任务按原有规则继续。');controls();}
 setTimeout(poll,900);
}
setInterval(controls,500);poll();
