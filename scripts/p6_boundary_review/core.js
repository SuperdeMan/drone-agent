/* Review validation and active-time accounting, without authority promotion. / 复核校验与活动计时，不提升记录权限。 */
(function(root){
  'use strict';
  const format='drone.p6-boundary-operator-reviews/v1';
  const common=['case_id','image_sha256','support_sha256','reviewer','notes','origin','started_at','completed_at','active_review_ms','ai_hints_viewed','source_support_viewed'];
  const roadKeys=[...common,'revision','decision','geometry'];
  const entityKeys=[...common,'entity_id','road_revision','entity_kind','target_relation','group_confirmed','geometry_source','geometry'];
  const equal=(a,b)=>JSON.stringify(normalize(a))===JSON.stringify(normalize(b));
  function normalize(v){return Array.isArray(v)?v.map(normalize):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,normalize(v[k])])):v;}
  function exact(v,keys){return v&&typeof v==='object'&&!Array.isArray(v)&&equal(Object.keys(v).sort(),[...keys].sort());}
  function polygonValid(p){
    if(!Array.isArray(p)||p.length<3||p.length>500||!p.every(a=>Array.isArray(a)&&a.length===2&&a.every(x=>typeof x==='number'&&Number.isFinite(x)&&x>=0&&x<=1)))return false;
    const cross=(a,b,c)=>(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]);
    const on=(a,b,c)=>Math.abs(cross(a,b,c))<1e-12&&c[0]>=Math.min(a[0],b[0])-1e-12&&c[0]<=Math.max(a[0],b[0])+1e-12&&c[1]>=Math.min(a[1],b[1])-1e-12&&c[1]<=Math.max(a[1],b[1])+1e-12;
    let area=0;
    for(let i=0;i<p.length;i++){
      const a=p[i],b=p[(i+1)%p.length];if(Math.hypot(a[0]-b[0],a[1]-b[1])<1e-12)return false;
      area+=a[0]*b[1]-b[0]*a[1];
      for(let j=i+2;j<p.length;j++){
        if(i===0&&j===p.length-1)continue;const c=p[j],d=p[(j+1)%p.length];
        if((cross(a,b,c)*cross(a,b,d)<0&&cross(c,d,a)*cross(c,d,b)<0)||on(a,b,c)||on(a,b,d)||on(c,d,a)||on(c,d,b))return false;
      }
    }
    return Math.abs(area)>1e-12;
  }
  function geometryValid(g,emptyCore=false){return exact(g,['core','outer'])&&Array.isArray(g.core)&&Array.isArray(g.outer)&&g.outer.length>0&&(emptyCore||g.core.length>0)&&g.core.length<=100&&g.outer.length<=100&&[...g.core,...g.outer].every(polygonValid);}
  function checkCommon(r,t,keys){
    const errors=[];
    if(!exact(r,keys))return ['记录字段不完整或包含未声明字段'];
    if(!t||['case_id','image_sha256','support_sha256'].some(k=>r[k]!==t[k]))errors.push('原图、支持域或目标身份不匹配');
    if(typeof r.reviewer!=='string'||!r.reviewer.trim()||r.reviewer.trim().length>120)errors.push('请填写复核人');
    if(typeof r.notes!=='string'||!r.notes.trim()||r.notes.trim().length>4000)errors.push('请填写判断依据');
    if(!['operator_entered','qa_fixture'].includes(r.origin)||(r.origin==='qa_fixture')!==(r.reviewer==='QA_FIXTURE_NOT_HUMAN'))errors.push('QA 夹具来源必须显式标记');
    if(['ai_hints_viewed','source_support_viewed'].some(k=>typeof r[k]!=='boolean'))errors.push('提示查看记录不完整');
    const timestamps=[r.started_at,r.completed_at];
    if(timestamps.some(t=>typeof t!=='string'||!/(Z|[+-]\d\d:\d\d)$/.test(t)||!Number.isFinite(Date.parse(t))))errors.push('请先开始复核');
    const elapsed=Date.parse(r.completed_at)-Date.parse(r.started_at);
    if(!Number.isInteger(r.active_review_ms)||r.active_review_ms<0||r.active_review_ms>elapsed+1000||elapsed<0)errors.push('活动计时与记录时间不一致');
    return errors;
  }
  function checkRoad(r,t){
    const errors=checkCommon(r,t,roadKeys);if(errors.length)return errors;
    if(typeof r.revision!=='string'||!(/^[A-Za-z0-9-]{8,80}$/).test(r.revision))errors.push('道路版本无效');
    if(!['accepted_draft','revised','unknown'].includes(r.decision))errors.push('请选择道路复核结果');
    if(r.decision==='unknown'){if(r.geometry!==null)errors.push('道路不确定时不可确认几何');}
    else if(!geometryValid(r.geometry))errors.push('请画出有效的道路内域和可能外域');
    if(r.decision==='accepted_draft'&&(!r.ai_hints_viewed||!equal(r.geometry,t.draft_geometry)))errors.push('采用草稿需要查看提示并保留其几何');
    return errors;
  }
  function checkEntity(r,t,road){
    const errors=checkCommon(r,t,entityKeys);if(errors.length)return errors;
    const entity=t.entities.find(e=>e.entity_id===r.entity_id);
    if(!entity)return ['实体不属于此目标'];
    if(!road||r.road_revision!==road.revision)return ['道路尚未登记或已变更，请重新复核本项'];
    if(r.origin!==road.origin)errors.push('QA 与操作员记录不能混用道路版本');
    if(!['physical','shadow','surface','unknown'].includes(r.entity_kind))errors.push('请选择实体性质');
    if(!['inside','outside','edge_uncertain','unknown'].includes(r.target_relation))errors.push('请选择与目标的关系');
    if(typeof r.group_confirmed!=='boolean')errors.push('请核对实体分组');
    if(r.entity_kind==='physical'){
      if(!geometryValid(r.geometry,r.target_relation!=='inside'))errors.push('请圈出可见实体和可能范围');
      if(!['ai_draft','drawn'].includes(r.geometry_source))errors.push('实体几何缺少来源');
      if(r.geometry_source==='ai_draft'&&(!r.ai_hints_viewed||!equal(r.geometry,entity.draft_geometry)))errors.push('采用草稿需要记录提示暴露');
    }else if(r.geometry!==null||r.geometry_source!==null||r.target_relation!=='unknown')errors.push('非实体或性质不明时不填写遮挡轮廓及位置结论');
    if(road.decision==='unknown'&&r.target_relation!=='unknown')errors.push('道路不确定时，位置关系须保留不确定');
    return errors;
  }
  function makeExport(bundle,roads,entities){
    const seen=new Set(),byCase=new Map();
    for(const r of roads){const errors=checkRoad(r,bundle.targets.find(t=>t.case_id===r.case_id));if(seen.has(r.case_id))errors.push('道路记录重复');if(errors.length)throw Error(errors.join('；'));seen.add(r.case_id);byCase.set(r.case_id,r);}
    seen.clear();
    for(const r of entities){const errors=checkEntity(r,bundle.targets.find(t=>t.case_id===r.case_id),byCase.get(r.case_id));if(seen.has(r.entity_id))errors.push('实体记录重复');if(errors.length)throw Error(errors.join('；'));seen.add(r.entity_id);}
    return {format,bundle_id:bundle.bundle_id,policy_id:bundle.policy_id,formal_release_eligible:false,reviewer_identity_authenticated:false,roads,entities};
  }
  function parseImport(text,bundle){
    const v=JSON.parse(text);
    if(!exact(v,['format','bundle_id','policy_id','formal_release_eligible','reviewer_identity_authenticated','roads','entities'])||v.format!==format||v.bundle_id!==bundle.bundle_id||v.policy_id!==bundle.policy_id||v.formal_release_eligible!==false||v.reviewer_identity_authenticated!==false||!Array.isArray(v.roads)||!Array.isArray(v.entities)||v.roads.length>bundle.targets.length||v.entities.length>bundle.targets.reduce((n,t)=>n+t.entities.length,0))throw Error('文件格式、素材包或开发权限标记不匹配');
    return makeExport(bundle,v.roads,v.entities);
  }
  class ActiveClock{
    constructor(elapsed=0){this.elapsed=elapsed;this.running=false;this.last=0;this.activity=0;this.eligible=false;}
    tick(now){if(this.running&&this.eligible)this.elapsed+=Math.max(0,Math.min(now,this.activity+60000)-this.last);this.last=now;return this.elapsed;}
    setEligible(now,value){this.tick(now);this.eligible=value;}
    touch(now){this.tick(now);this.activity=now;}
    start(now,eligible){this.last=now;this.activity=now;this.eligible=eligible;this.running=true;}
    pause(now){this.tick(now);this.running=false;}
  }
  const api={equal,exact,polygonValid,geometryValid,checkRoad,checkEntity,makeExport,parseImport,ActiveClock};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.BoundaryCore=api;
})(typeof window==='undefined'?globalThis:window);
