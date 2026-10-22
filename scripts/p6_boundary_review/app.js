/* Local-only review workflow with explicit AI exposure and road revisions. / 仅本地复核，显式记录 AI 提示暴露与道路版本。 */
(function(){
  'use strict';
  const B=window.BOUNDARY_DATA,C=window.BoundaryCore,$=id=>document.getElementById(id),NS='http://www.w3.org/2000/svg';
  const clone=v=>JSON.parse(JSON.stringify(v));
  const items=B.targets.flatMap(t=>[{target:t,entity:null,id:t.case_id},...t.entities.map(e=>({target:t,entity:e,id:e.entity_id}))]);
  const key='p6-boundary:'+B.bundle_id;
  let roads=[],entities=[],drafts={},index=0,clock=new C.ActiveClock(),drawing=false,points=[],view=[],pan=null;
  let showAI=false,showSupport=false,showBoxes=false,imageReady=false,reviewer='';
  const current=()=>items[index],target=()=>current().target,entity=()=>current().entity;
  const saved=()=>entity()?entities.find(e=>e.entity_id===current().id):roads.find(r=>r.case_id===current().id);
  const currentRoad=()=>roads.find(r=>r.case_id===target().case_id);
  function fresh(){return {reviewer,notes:'',started_at:null,active_review_ms:0,ai_hints_viewed:false,source_support_viewed:false,decision:'',entity_kind:'',target_relation:'',group_confirmed:false,geometry:null,geometry_source:null,dirty:false};}
  function draft(){if(!drafts[current().id])drafts[current().id]=saved()?{...fresh(),...clone(saved())}:fresh();return drafts[current().id];}
  function draftValid(d){return d&&typeof d==='object'&&typeof d.notes==='string'&&typeof d.reviewer==='string'&&Number.isInteger(d.active_review_ms)&&d.active_review_ms>=0&&typeof d.ai_hints_viewed==='boolean'&&typeof d.source_support_viewed==='boolean'&&typeof d.group_confirmed==='boolean'&&(d.started_at===null||Number.isFinite(Date.parse(d.started_at)))&&(d.geometry===null||(C.exact(d.geometry,['core','outer'])&&['core','outer'].every(k=>Array.isArray(d.geometry[k])&&d.geometry[k].length<=100&&d.geometry[k].every(C.polygonValid))));}
  function message(text){$('message').textContent=text;}
  try{
    const v=JSON.parse(localStorage.getItem(key)||'null');
    if(v){
      // Cache may contain stale dependants; validate roads and each matching entity separately. / 缓存可保留过期依赖，分别核对道路与仍匹配的实体。
      const r=C.makeExport(B,v.roads||[],[]);roads=r.roads;
      for(const e of v.entities||[]){const t=B.targets.find(t=>t.case_id===e.case_id),road=roads.find(r=>r.case_id===e.case_id);if(t&&road){const probe={...e,road_revision:road.revision};if(!C.checkEntity(probe,t,road).length)entities.push(e);}}
      if(typeof v.reviewer==='string')reviewer=v.reviewer;
      for(const [id,d] of Object.entries(v.drafts||{}))if(items.some(i=>i.id===id)&&draftValid(d))drafts[id]=d;
    }
  }catch(e){message('本机缓存未通过校验，请从导出的 JSON 恢复。');roads=[];entities=[];drafts={};}
  function persist(){
    draft().active_review_ms=Math.floor(clock.elapsed);
    try{localStorage.setItem(key,JSON.stringify({roads,entities,drafts,reviewer}));$('storageStatus').textContent='草稿已保存在本机 · 请导出 JSON 留存';}
    catch(e){$('storageStatus').textContent='本机缓存不可用，请及时导出已登记记录';}
  }
  function sync(){
    const d=draft();d.reviewer=$('reviewer').value;reviewer=d.reviewer;d.notes=$('notes').value;d.decision=$('roadDecision').value;d.entity_kind=$('entityKind').value;d.target_relation=$('targetRelation').value;d.group_confirmed=$('groupConfirmed').checked;
  }
  function tick(){clock.tick(performance.now());draft().active_review_ms=Math.floor(clock.elapsed);const sec=Math.floor(clock.elapsed/1000);$('elapsed').textContent=String(Math.floor(sec/60)).padStart(2,'0')+':'+String(sec%60).padStart(2,'0');$('start').textContent=clock.running?'暂停计时':'开始 / 继续复核';}
  function pause(){clock.pause(performance.now());tick();persist();}
  const eligible=()=>!document.hidden&&document.hasFocus();
  ['pointerdown','pointermove','keydown'].forEach(name=>document.addEventListener(name,()=>clock.touch(performance.now()),{passive:true}));
  document.addEventListener('visibilitychange',()=>{clock.setEligible(performance.now(),eligible());persist();});
  window.addEventListener('blur',()=>{clock.setEligible(performance.now(),false);persist();});
  window.addEventListener('focus',()=>clock.setEligible(performance.now(),eligible()));
  window.addEventListener('beforeunload',()=>{sync();pause();});
  setInterval(()=>{tick();persist();},1000);
  function status(i){const r=i.entity?entities.find(r=>r.entity_id===i.id):roads.find(r=>r.case_id===i.id);if(!r)return '待复核';if(i.entity&&r.road_revision!==roads.find(r=>r.case_id===i.target.case_id)?.revision)return '道路已变更';return '已登记';}
  function navigation(){
    const root=$('queue');root.replaceChildren();
    B.targets.forEach((t,n)=>{const g=document.createElement('div');g.className='target-group';const label=document.createElement('div');label.className='target-label';label.textContent=String(n+1).padStart(2,'0')+' / '+t.image_id;g.append(label);
      items.forEach((it,i)=>{if(it.target!==t)return;const b=document.createElement('button');b.className=(i===index?'active ':'')+(status(it)==='道路已变更'?'stale':'');b.setAttribute('aria-current',i===index?'step':'false');b.dataset.item=it.id;const text=document.createElement('span'),s=document.createElement('em');text.textContent=it.entity?it.entity.entity_id+' 实体':'道路范围';s.textContent=status(it);b.append(text,s);b.onclick=()=>select(i);g.append(b);});root.append(g);});
    const count=items.filter(i=>status(i)==='已登记').length;$('progress').textContent=count+' / '+items.length;$('progressBar').style.width=(100*count/items.length)+'%';
  }
  function select(i,skipSync=false){
    if(!skipSync){sync();pause();}index=i;clock=new C.ActiveClock(draft().active_review_ms);drawing=false;points=[];showAI=false;showSupport=false;showBoxes=false;pan=null;$('hintPanel').open=false;
    const t=target(),e=entity(),d=draft();$('itemNumber').textContent=(e?'02 / ENTITY':'01 / ROAD')+' · '+String(index+1).padStart(2,'0')+' / '+items.length;
    $('itemTitle').textContent=e?e.entity_id+' · 实体复核':t.image_id+' · 道路范围';$('itemState').textContent=status(current());
    $('stepHelp').textContent=e?'确认实体性质，再判断其是否进入已复核道路。':'圈出确定内域与可能外域。若道路边界无法确定，请保留不确定。';
    $('roadFields').hidden=!!e;$('entityFields').hidden=!e;$('toggleBoxes').hidden=!e;
    $('reviewer').value=d.reviewer||reviewer;$('notes').value=d.notes;$('roadDecision').value=d.decision;$('entityKind').value=d.entity_kind;$('targetRelation').value=d.target_relation;$('groupConfirmed').checked=d.group_confirmed;
    $('roadDependency').textContent=currentRoad()?'道路已登记；修改道路后本项须重新登记。':'请先在左侧登记此目标的道路范围。';
    if(e&&currentRoad()?.geometry&&currentRoad().ai_hints_viewed)d.ai_hints_viewed=true;
    $('aiNotes').textContent=e?e.ai_description+'。'+e.ai_notes+' 条件几何：'+e.ai_relation:t.ai_road_description;
    $('sourceRefs').textContent=e?'来源候选（框仅用于定位）：'+e.candidate_refs.map((r,n)=>(n+1)+'. '+r.split(':').slice(1).join(':')).join('；'):'';
    $('photo').setAttribute('width',t.width);$('photo').setAttribute('height',t.height);$('photo').setAttribute('href',t.image_file);
    imageReady=false;const selectedId=current().id;const im=new Image();im.onload=()=>{if(current().id===selectedId){imageReady=im.naturalWidth===t.width&&im.naturalHeight===t.height;if(!imageReady)message('图片尺寸不匹配，禁止登记。');}};im.onerror=()=>{if(current().id===selectedId)message('原图无法载入，禁止登记。');};im.src=t.image_file;
    view=[0,0,t.width,t.height];message('');updateView();drawLayers();navigation();tick();
  }
  function markAI(){draft().ai_hints_viewed=true;const r=saved();if(r)r.ai_hints_viewed=true;persist();}
  function svg(tag,attrs,parent=$('layers')){const n=document.createElementNS(NS,tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);parent.append(n);return n;}
  function shape(geometry,color,opacity=1){if(!geometry)return;for(const layer of ['outer','core'])for(const p of geometry[layer])svg('polygon',{points:p.map(([x,y])=>(x*(target().width-1))+','+(y*(target().height-1))).join(' '),fill:color,'fill-opacity':layer==='core'?0.1*opacity:0,stroke:layer==='core'?color:'#f8be49','stroke-width':layer==='core'?2:1.5,'stroke-opacity':opacity,'stroke-dasharray':layer==='outer'?'6 4':'none','vector-effect':'non-scaling-stroke'});}
  function drawLayers(){
    const t=target(),e=entity(),d=draft();$('layers').replaceChildren();
    if(showSupport)svg('image',{href:t.support_file,width:t.width,height:t.height,opacity:.38,style:'mix-blend-mode:screen'});
    if(e&&currentRoad()?.geometry)shape(currentRoad().geometry,'#59a8ff',.6);
    if(showAI)shape((e||t).draft_geometry,'#e797ff',.7);
    if(showBoxes&&e)e.candidate_boxes.forEach((b,n)=>{svg('rect',{x:b[0]*t.width,y:b[1]*t.height,width:(b[2]-b[0])*t.width,height:(b[3]-b[1])*t.height,fill:'none',stroke:'#fff','stroke-width':1.5,'stroke-dasharray':'3 5','vector-effect':'non-scaling-stroke'});const label=svg('text',{x:b[0]*t.width,y:b[1]*t.height,'font-size':view[2]/40,fill:'#fff',stroke:'#111','stroke-width':view[2]/3000,'paint-order':'stroke'});label.textContent=String(n+1);});
    shape(d.geometry,'#3ce7ba');
    if(points.length){svg('polyline',{points:points.map(([x,y])=>(x*(t.width-1))+','+(y*(t.height-1))).join(' '),fill:'none',stroke:'#fff','stroke-width':2,'vector-effect':'non-scaling-stroke'});for(const [x,y]of points)svg('circle',{cx:x*(t.width-1),cy:y*(t.height-1),r:view[2]/180,fill:'#fff'});}
    $('geometryStatus').textContent=d.geometry?'当前：'+d.geometry.core.length+' 个内域轮廓 / '+d.geometry.outer.length+' 个可能外域轮廓。外域必须包含内域。':'当前未填写几何。';
    $('drawState').textContent=drawing?'圈绘中 · '+points.length+' 点，Enter 闭合':'浏览模式 · 滚轮缩放，拖动画面';
    $('canvas').classList.toggle('drawing',drawing);$('finish').disabled=!drawing;$('undo').disabled=!points.length;$('cancel').disabled=!drawing;
    $('toggleAI').textContent=showAI?'隐藏 AI 轮廓':'显示 AI 轮廓';$('toggleSupport').textContent=showSupport?'隐藏源支持域':'显示源支持域';$('toggleBoxes').textContent=showBoxes?'隐藏来源框':'显示来源框';
  }
  function updateView(){$('canvas').setAttribute('viewBox',view.join(' '));$('scale').textContent=(target().width/view[2]).toFixed(1)+'× 全图';}
  function point(event){const p=$('canvas').createSVGPoint();p.x=event.clientX;p.y=event.clientY;const q=p.matrixTransform($('canvas').getScreenCTM().inverse());return [q.x,q.y];}
  function zoom(factor,center=null){const t=target(),[x,y,w,h]=view;const next=Math.max(t.width/40,Math.min(t.width*1.5,w*factor)),ratio=next/w;const [cx,cy]=center||[x+w/2,y+h/2];view=[cx-(cx-x)*ratio,cy-(cy-y)*ratio,next,h*ratio];updateView();drawLayers();}
  $('zoomIn').onclick=()=>zoom(.65);$('zoomOut').onclick=()=>zoom(1/.65);$('fit').onclick=()=>{view=[0,0,target().width,target().height];updateView();drawLayers();};
  $('locate').onclick=()=>{const d=draft(),g=d.geometry?.outer.length?d.geometry:(entity()||target()).draft_geometry;if(g!==d.geometry)markAI();const pts=g.outer.flat();const xs=pts.map(p=>p[0]*target().width),ys=pts.map(p=>p[1]*target().height);const cx=(Math.min(...xs)+Math.max(...xs))/2,cy=(Math.min(...ys)+Math.max(...ys))/2;const w=Math.max(160,(Math.max(...xs)-Math.min(...xs))*1.5,(Math.max(...ys)-Math.min(...ys))*1.5*target().width/target().height);view=[cx-w/2,cy-w*target().height/target().width/2,w,w*target().height/target().width];updateView();drawLayers();};
  $('canvas').addEventListener('wheel',ev=>{ev.preventDefault();zoom(ev.deltaY>0?1.15:1/1.15,point(ev));},{passive:false});
  $('canvas').addEventListener('pointerdown',ev=>{if(ev.button!==0)return;const p=point(ev);if(drawing){const normalized=[p[0]/(target().width-1),p[1]/(target().height-1)];if(normalized.every(v=>v>=0&&v<=1)&&points.length<500)points.push(normalized);drawLayers();}else{pan={screen:[ev.clientX,ev.clientY],view:[...view]};$('canvas').setPointerCapture(ev.pointerId);}});
  $('canvas').addEventListener('pointermove',ev=>{if(!pan)return;const matrix=$('canvas').getScreenCTM();view=[pan.view[0]-(ev.clientX-pan.screen[0])/matrix.a,pan.view[1]-(ev.clientY-pan.screen[1])/matrix.d,...pan.view.slice(2)];updateView();});
  ['pointerup','pointercancel'].forEach(name=>$('canvas').addEventListener(name,()=>{pan=null;}));
  function edited(){draft().dirty=true;if(entity()){draft().geometry_source='drawn';}else{draft().decision='revised';$('roadDecision').value='revised';}drawLayers();persist();}
  $('draw').onclick=()=>{drawing=true;points=[];drawLayers();$('viewport').focus();};
  $('finish').onclick=()=>{if(!C.polygonValid(points)){message('轮廓至少需要三个不共线点，且不能自交。');return;}const d=draft();if(!d.geometry)d.geometry={core:[],outer:[]};const layer=$('drawLayer').value;if(d.geometry[layer].length>=100){message('每层最多 100 个轮廓。');return;}d.geometry[layer].push(clone(points));drawing=false;points=[];edited();message('已加入轮廓；仍需登记判断。');};
  $('undo').onclick=()=>{points.pop();drawLayers();};$('cancel').onclick=()=>{drawing=false;points=[];drawLayers();};
  $('removePolygon').onclick=()=>{draft().geometry?.[$('drawLayer').value].pop();edited();};
  $('viewport').addEventListener('keydown',ev=>{if(ev.key==='Enter'&&drawing){ev.preventDefault();$('finish').click();}if(ev.key==='Escape')$('cancel').click();});
  $('hintPanel').addEventListener('toggle',()=>{if($('hintPanel').open)markAI();});
  $('toggleAI').onclick=()=>{markAI();showAI=!showAI;drawLayers();};$('toggleBoxes').onclick=()=>{markAI();showBoxes=!showBoxes;drawLayers();};
  $('toggleSupport').onclick=()=>{draft().source_support_viewed=true;const r=saved();if(r)r.source_support_viewed=true;showSupport=!showSupport;drawLayers();persist();};
  $('adopt').onclick=()=>{markAI();draft().dirty=true;draft().geometry=clone((entity()||target()).draft_geometry);if(entity())draft().geometry_source='ai_draft';else{draft().decision='accepted_draft';$('roadDecision').value='accepted_draft';}drawLayers();persist();message('已复制 AI 几何，请核对后填写结论并登记。');};
  ['reviewer','notes','roadDecision','entityKind','targetRelation','groupConfirmed'].forEach(id=>$(id).addEventListener('input',()=>{sync();draft().dirty=true;persist();}));
  $('start').onclick=()=>{if(clock.running)pause();else{sync();if(!draft().started_at)draft().started_at=new Date().toISOString();clock.start(performance.now(),eligible());tick();persist();}};
  $('save').onclick=()=>{
    sync();tick();if(!imageReady){message('请等待原图完整载入后再登记。');return;}if(drawing){message('请先闭合或取消正在画的轮廓。');return;}
    const d=draft(),t=target(),e=entity();const r={case_id:t.case_id,image_sha256:t.image_sha256,support_sha256:t.support_sha256,reviewer:d.reviewer.trim(),notes:d.notes.trim(),origin:d.reviewer.trim()==='QA_FIXTURE_NOT_HUMAN'?'qa_fixture':'operator_entered',started_at:d.started_at,completed_at:new Date().toISOString(),active_review_ms:Math.floor(clock.elapsed),ai_hints_viewed:d.ai_hints_viewed,source_support_viewed:d.source_support_viewed};
    let errors;
    if(e){Object.assign(r,{entity_id:e.entity_id,road_revision:currentRoad()?.revision||'',entity_kind:d.entity_kind,target_relation:d.target_relation,group_confirmed:d.group_confirmed,geometry_source:d.entity_kind==='physical'?d.geometry_source:null,geometry:d.entity_kind==='physical'?clone(d.geometry):null});errors=C.checkEntity(r,t,currentRoad());}
    else{Object.assign(r,{revision:crypto.randomUUID(),decision:d.decision,geometry:d.decision==='unknown'?null:clone(d.geometry)});errors=C.checkRoad(r,t);}
    if(errors.length){message(errors.join('；'));return;}
    if(e)entities=[...entities.filter(v=>v.entity_id!==e.entity_id),r];else roads=[...roads.filter(v=>v.case_id!==t.case_id),r];d.dirty=false;
    pause();navigation();$('itemState').textContent='已登记';message(e?'本项已登记。导出后仍需完整几何校验。':'道路已登记，关联实体需按此版本复核。');persist();
  };
  $('next').onclick=()=>select(Math.min(index+1,items.length-1));
  $('exportButton').onclick=()=>{try{sync();pause();const data=C.makeExport(B,roads,entities),blob=new Blob([JSON.stringify(data,null,2)+'\n'],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='p6-boundary-reviews-'+new Date().toISOString().replace(/[:.]/g,'-')+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),2000);message('已导出 '+roads.length+' 项道路、'+entities.length+' 项实体。未登记草稿不计入导出。');}catch(e){message(e.message);}};
  $('importButton').onclick=()=>$('importFile').click();
  $('importFile').onchange=async()=>{const file=$('importFile').files[0];if(!file)return;try{if(file.size>5000000)throw Error('导入文件超过 5 MB');const incoming=C.parseImport(await file.text(),B);sync();pause();const mergedRoads=[...roads],mergedEntities=[...entities];for(const [records,added,id]of [[mergedRoads,incoming.roads,'case_id'],[mergedEntities,incoming.entities,'entity_id']])for(const r of added){const previous=records.find(p=>p[id]===r[id]);if(drafts[r[id]]?.dirty||(previous&&!C.equal(previous,r)))throw Error('导入与本机记录或未登记草稿冲突：'+r[id]+'。请在独立浏览器会话中校验，保留两份原始导出。');if(!previous)records.push(r);}C.makeExport(B,mergedRoads,mergedEntities);roads=mergedRoads;entities=mergedEntities;for(const r of [...incoming.roads,...incoming.entities])drafts[r.entity_id||r.case_id]={...fresh(),...clone(r)};select(index,true);message('导入通过结构校验；来源字节与栅格几何仍需离线校验。');persist();}catch(e){message('导入失败：'+e.message);}finally{$('importFile').value='';}};
  $('bundleLabel').textContent='BUNDLE '+B.bundle_id.slice(0,12);
  // Initialize without overwriting a recovered draft from empty controls. / 初始化时不让空表单覆盖已恢复草稿。
  const first=draft();$('reviewer').value=first.reviewer||reviewer;$('notes').value=first.notes;$('roadDecision').value=first.decision;$('entityKind').value=first.entity_kind;$('targetRelation').value=first.target_relation;$('groupConfirmed').checked=first.group_confirmed;clock=new C.ActiveClock(first.active_review_ms);
  select(0,true);
})();
