/* Read-only navigation; no model calls, storage or human annotation writes. / 只读导航，不调用模型、不写缓存或人工标注。 */
(function(){
  'use strict';
  const B=window.PROPOSAL_DATA,C=window.ProposalCore,$=id=>document.getElementById(id),NS='http://www.w3.org/2000/svg';
  const groups={candidate:'候选 · 模型开发分组',outside_target:'范围外 · 模型开发分组',needs_review:'待复核 · 模型开发分组',non_entity:'非实体 · 模型开发分组'};
  const reasons={
    candidate_window_disjoint_from_provisional_possible_road:'窗口与既有可能道路草稿无交叠，需核对它是否真的位于所选道路。',
    response_quarantined:'原回答未通过协议校验，保留窗口待核对。',
    vehicle_presence_does_not_establish_obstruction:'画面中有车辆；静态出现尚不足以认定障碍。',
    physical_nature_unresolved:'实体性质尚不明确，需区分实体、阴影与路面表象。',
    selected_road_relation_unresolved:'对象与所选道路的关系尚不明确。',
    decision_uncertainty_remains:'模型仍保留会影响候选判断的不确定性。',
    ai_visual_followup_distinguish_small_entity_from_road_marking:'需辨认小型实体、道路标记或表面变化；这是 AI 开发复核线索。',
    unconfirmed_model_group:'该模型分组尚未经独立人工确认。'
  };
  const kinds={physical_object:'可见实体',vehicle:'车辆',shadow:'阴影',surface_change:'表面变化',unknown:'未知'};
  const relations={intersects:'声明与道路相交',outside:'声明位于范围外',uncertain:'道路关系不明',unknown:'道路关系未知'};
  const locations={single_entity:'单件声明',entity_group:'实体群声明',unresolved:'实例边界未分离',not_applicable:'不适用'};
  let current=null,visible=[],mode='original',view=[],loadGeneration=0,pan=null;
  const target=()=>B.targets.find(t=>t.view_id===current?.view_id);
  const shorthand=r=>r.proposal_id.split('-').at(-1).toUpperCase();
  const showReason=reason=>reasons[reason]||'其它开发复核线索：'+reason;
  function node(tag,attrs={},text){
    const el=document.createElementNS(NS,tag);for(const [k,v] of Object.entries(attrs))el.setAttribute(k,String(v));
    if(text!==undefined)el.textContent=text;return el;
  }
  function updateLabels(){
    for(const id of ['overview','detail']){
      const svg=$(id),matrix=svg.getScreenCTM();if(!matrix)continue;
      const scale=Math.hypot(matrix.a,matrix.b);if(!scale)continue;
      const width=svg.getBoundingClientRect().width;if(!width||!svg.clientWidth)continue;
      const cssZoom=width/svg.clientWidth;
      for(const text of svg.querySelectorAll('.proposal-label')){
        const font=12*cssZoom/scale;text.setAttribute('font-size',font);text.setAttribute('y',Math.max(font,Number(text.dataset.top)-font*.4));
        text.setAttribute('x',Math.max(0,Number(text.dataset.left)));
      }
    }
  }
  function revealSelection(){
    const queue=$('queue'),active=queue.querySelector('button.active');if(!active)return;
    const before=queue.getBoundingClientRect();if(!queue.offsetWidth)return;
    const zoom=before.width/queue.offsetWidth;
    const available=document.body.clientWidth<=700?150:Math.max(40,Math.min(640,Math.floor((innerHeight-Math.max(before.top,0)-12)/zoom)));
    queue.style.maxHeight=available+'px';
    const q=queue.getBoundingClientRect(),r=active.getBoundingClientRect();
    if(!queue.offsetWidth||!queue.offsetHeight)return;
    const sx=q.width/queue.offsetWidth,sy=q.height/queue.offsetHeight;
    const top=Math.max(q.top,0),left=Math.max(q.left,0);
    const bottom=Math.min(q.top+queue.clientHeight*sy,innerHeight),right=Math.min(q.left+queue.clientWidth*sx,innerWidth);
    if(bottom-top>=r.height&&(r.top<top||r.bottom>bottom))queue.scrollTop+=((r.top+r.bottom)-(top+bottom))/(2*sy);
    if(right-left>=r.width&&(r.left<left||r.right>right))queue.scrollLeft+=((r.left+r.right)-(left+right))/(2*sx);
  }
  function paintBoxes(){
    if(!current)return;
    const t=target(),selected=current.proposal_id;
    for(const [layer,rows] of [[$('overviewBoxes'),B.proposals.filter(r=>r.view_id===t.view_id&&($('showAll').checked||r.proposal_id===selected))],[$('detailBoxes'),[current]]]){
      layer.replaceChildren();
      for(const row of rows){
        const [x,y,w,h]=C.rect(row.source_bbox,t.size),active=row.proposal_id===selected;
        const box=node('rect',{x,y,width:w,height:h,class:'proposal-box'+(active?' selected':''),'data-proposal':row.proposal_id});
        if(layer.id==='overviewBoxes'){
          box.setAttribute('role','button');box.setAttribute('tabindex','0');box.setAttribute('aria-label','查看提案 '+shorthand(row));
          box.addEventListener('click',()=>select(row));box.addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();select(row);}});
        }
        const label=node('text',{class:'proposal-label','data-left':x,'data-top':y},shorthand(row));
        layer.append(box,label);
      }
    }
    updateLabels();
  }
  function setView(next){view=next;$('detail').setAttribute('viewBox',view.join(' '));updateLabels();}
  async function loadImages(){
    const generation=++loadGeneration,t=target(),image=t.modes[mode];
    $('imageStatus').textContent='正在载入图像…';$('detailFrame').dataset.ready='false';
    for(const id of ['overviewPhoto','detailPhoto']){
      $(id).setAttribute('href',image.file);$(id).setAttribute('width',t.size[0]);$(id).setAttribute('height',t.size[1]);
    }
    $('resolution').textContent=image.size.join(' × ')+' px';
    $('modeNote').textContent={original:'当前为原始分辨率图。提案仍来自缩小后的模型输入；更多像素不代表已有人工真值。',model:'当前为实际发送给模型的原图字节，可对照当时可见的细节。',support:'当前为实际发送给模型的道路提示图。青色仅是暂定支持范围，孔洞不能直接视作道路外。'}[mode];
    try{
      const probe=new Image();probe.src=image.file;await probe.decode();
      if(generation!==loadGeneration)return;
      $('imageStatus').textContent='图像已载入 · 仅查看，人工确认数保持 0';$('detailFrame').dataset.ready='true';
    }catch(e){if(generation===loadGeneration)$('imageStatus').textContent='图像载入失败，请核对本地包及来源摘要。';}
  }
  function references(){
    const obs=current.model_observation;$('modelGroup').textContent=groups[current.model_disposition]||current.model_disposition;
    $('observation').replaceChildren();
    const fields=obs?[['性质与位置',(kinds[obs.entity_kind]||obs.entity_kind)+' / '+(relations[obs.target_relation]||obs.target_relation)],['定位说明',locations[obs.localization_status]||obs.localization_status],['实体依据',obs.entity_evidence],['道路关系依据',obs.relation_evidence],['影响判断的不确定性',obs.decision_uncertainty||'原回答未填写'],['描述层面的限制',obs.description_uncertainty||'原回答未填写']]:[['原回答状态','未通过校验，不展示为有效观察。']];
    for(const [label,text] of fields){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=text;$('observation').append(dt,dd);}
    $('reviewReasons').replaceChildren();for(const reason of current.review_reasons){const li=document.createElement('li');li.textContent=showReason(reason);$('reviewReasons').append(li);}
    $('geometryNote').textContent='条件几何：扩展窗口内，源支持域 '+current.conditional_geometry.source_support_pixels+' 像素；可能道路范围 '+current.conditional_geometry.possible_road_pixels+' 像素。该数值不表示物体面积或业务真值。';
  }
  function navigation(){
    $('queue').replaceChildren();$('filteredCount').textContent=visible.length+' / '+B.proposals.length;
    for(const t of B.targets){
      const rows=visible.filter(r=>r.view_id===t.view_id);if(!rows.length)continue;
      const section=document.createElement('div');section.className='queue-section';
      const heading=document.createElement('h3');heading.textContent=t.title;section.append(heading);
      for(const row of rows){
        const button=document.createElement('button'),name=document.createElement('span'),state=document.createElement('small');
        button.dataset.proposal=row.proposal_id;button.className=(row.proposal_id===current?.proposal_id?'active ':'')+(row.priority==='first'?'priority':'');
        if(row.proposal_id===current?.proposal_id)button.setAttribute('aria-current','true');
        name.textContent=shorthand(row);state.textContent=row.priority==='first'?'优先':'待确认';button.append(name,state);button.onclick=()=>select(row);section.append(button);
      }
      $('queue').append(section);
    }
    requestAnimationFrame(revealSelection);
  }
  function select(row){
    if(row&&!visible.some(r=>r.proposal_id===row.proposal_id)){
      $('scope').value='';$('priorityOnly').checked=false;visible=C.filtered(B.proposals,'',false);
    }
    current=row;pan=null;navigation();$('active').hidden=!row;$('empty').hidden=!!row;
    if(!row){loadGeneration++;$('overviewPhoto').removeAttribute('href');$('detailPhoto').removeAttribute('href');return;}
    const t=target(),at=visible.findIndex(r=>r.proposal_id===row.proposal_id);
    $('targetLabel').textContent=t.title+' / '+(at+1)+' OF '+visible.length;
    $('currentTitle').textContent='提案 '+shorthand(row);$('proposalId').textContent=row.proposal_id;
    $('previous').disabled=at<=0;$('next').disabled=at>=visible.length-1;
    $('risk').hidden=row.priority!=='first';$('riskText').textContent=row.review_reasons.map(showReason).join(' ');
    $('coordinates').textContent='['+row.source_bbox.join(', ')+']';
    const [x,y,w,h]=C.rect(row.source_bbox,t.size);$('pixelBounds').textContent='原图窗口：'+[x,y,x+w,y+h].map(v=>v.toFixed(1)).join(' / ')+' px';
    $('overview').setAttribute('viewBox','0 0 '+t.size.join(' '));setView(C.focus(row.source_bbox,t.size));paintBoxes();references();loadImages();
    try{history.replaceState(null,'','#'+encodeURIComponent(row.proposal_id));}catch(e){/* File origins can restrict history. / 本地文件来源可能限制历史写入。 */}
  }
  function filter(){
    visible=C.filtered(B.proposals,$('scope').value,$('priorityOnly').checked);
    select(visible.find(r=>r.proposal_id===current?.proposal_id)||C.initial(visible));
  }
  for(const t of B.targets){const option=document.createElement('option');option.value=t.view_id;option.textContent=t.title;$('scope').append(option);}
  $('scope').onchange=filter;$('priorityOnly').onchange=filter;
  $('clearFilters').onclick=()=>{$('scope').value='';$('priorityOnly').checked=false;filter();};
  $('showAll').onchange=paintBoxes;
  document.querySelectorAll('input[name=mode]').forEach(input=>input.onchange=()=>{if(input.checked){mode=input.value;if(current)loadImages();}});
  $('context').onclick=()=>setView(C.focus(current.source_bbox,target().size));
  $('tight').onclick=()=>setView(C.focus(current.source_bbox,target().size,true));
  $('zoomIn').onclick=()=>setView(C.zoom(view,target().size,1/1.6));$('zoomOut').onclick=()=>setView(C.zoom(view,target().size,1.6));
  function step(amount){const index=visible.findIndex(r=>r.proposal_id===current?.proposal_id),next=visible[index+amount];if(next)select(next);}
  $('previous').onclick=()=>step(-1);$('next').onclick=()=>step(1);
  document.addEventListener('keydown',e=>{if(['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName))return;if(e.key==='ArrowLeft'){e.preventDefault();step(-1);}if(e.key==='ArrowRight'){e.preventDefault();step(1);}});
  function point(event,matrix){return new DOMPoint(event.clientX,event.clientY).matrixTransform(matrix||$('detail').getScreenCTM().inverse());}
  $('detailFrame').addEventListener('wheel',e=>{if(!current||e.ctrlKey)return;e.preventDefault();const p=point(e);setView(C.zoom(view,target().size,e.deltaY>0?1.25:.8,[p.x,p.y]));},{passive:false});
  $('detail').addEventListener('pointerdown',e=>{if(!current||e.button!==0)return;const inverse=$('detail').getScreenCTM().inverse();pan={point:point(e,inverse),inverse,view:[...view]};$('detail').setPointerCapture(e.pointerId);$('detail').classList.add('dragging');});
  $('detail').addEventListener('pointermove',e=>{if(!pan)return;const p=point(e,pan.inverse);setView(C.bounded([pan.view[0]-(p.x-pan.point.x),pan.view[1]-(p.y-pan.point.y),pan.view[2],pan.view[3]],target().size));});
  for(const name of ['pointerup','pointercancel','lostpointercapture'])$('detail').addEventListener(name,()=>{pan=null;$('detail').classList.remove('dragging');});
  const resize=new ResizeObserver(()=>{updateLabels();revealSelection();});
  resize.observe(document.querySelector('.workspace'));resize.observe($('queue'));
  window.addEventListener('scroll',()=>requestAnimationFrame(revealSelection),{passive:true});
  $('total').textContent=B.proposals.length;$('priorityTotal').textContent=B.proposals.filter(r=>r.priority==='first').length;
  $('bundleLabel').textContent='BUNDLE '+B.bundle_id.slice(0,12);
  function hashItem(){try{return B.proposals.find(r=>r.proposal_id===decodeURIComponent(location.hash.slice(1)));}catch(e){return null;}}
  window.addEventListener('hashchange',()=>{const row=hashItem();if(row&&row.proposal_id!==current?.proposal_id)select(row);});
  visible=[...B.proposals];select(hashItem()||C.initial(visible));
})();
