/* Validate whole-target annotations without model answers or authority promotion. / 校验完整目标标注，不引入模型答案或提升权限。 */
(function(root){
  'use strict';
  const shared=typeof module!=='undefined'&&module.exports?require('../p6_boundary_review/core.js'):root.BoundaryCore;
  const fields=['sample_id','image_sha256','revision','reviewer_id','origin','phase','started_at','completed_at','active_ms','target_brief','target_registered_at','target_polygon','target_confirmed','full_target_inspected','verdict','objects','notes','task_reference','model_outputs_seen','source_labels_seen','metadata_seen','asset_id','site_id','capture_batch_id','near_duplicate_group','grouping_verified','grouping_evidence'];
  const aiFields=['ai_draft_sha256','ai_draft_decision','ai_original_checked','evaluated_model_outputs_seen'];
  const expected=s=>s?.ai_suggestion?fields.concat(aiFields):fields;
  const canonical=v=>Array.isArray(v)?v.map(canonical):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,canonical(v[k])])):v;
  const unchanged=(r,s)=>['target_brief','target_polygon','verdict','objects','notes'].every(k=>JSON.stringify(canonical(r[k]))===JSON.stringify(canonical(s.ai_suggestion[k])));
  const kinds=['debris','vegetation','other_entity'],verdicts=['normal','obstacle','indeterminate','wrong_target','excluded_source'];
  const exact=shared.exact,text=(v,n,required=false)=>typeof v==='string'&&v.length<=n&&(!required||v.trim().length>0);
  const date=v=>typeof v==='string'&&/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(v)&&Number.isFinite(Date.parse(v));
  function validate(r,s,qa=false){
    const errors=[];
    if(!exact(r,expected(s)))return ['标注字段不完整或包含未知字段'];
    if(!s||r.sample_id!==s.sample_id||r.image_sha256!==s.image_sha256)errors.push('图片身份不匹配');
    if(r.origin!==(qa?'qa_fixture':'operator_entered')||r.phase!=='annotation')errors.push('标注来源或阶段不匹配');
    if(!Number.isInteger(r.revision)||r.revision<1||!verdicts.includes(r.verdict))errors.push('请选择结论');
    for(const [k,n,required]of [['reviewer_id',120,true],['notes',4000,true],['target_brief',600,false],['task_reference',600,false],['grouping_evidence',1000,false]])if(!text(r[k],n,required))errors.push('请补齐或缩短：'+k);
    if(!qa&&typeof r.reviewer_id==='string'&&r.reviewer_id.toUpperCase().startsWith('QA_'))errors.push('QA 身份不能登记为人工复核');
    for(const k of ['target_confirmed','full_target_inspected','model_outputs_seen','source_labels_seen','metadata_seen','grouping_verified'])if(typeof r[k]!=='boolean')errors.push('请明确确认：'+k);
    const wall=Date.parse(r.completed_at)-Date.parse(r.started_at);
    if(!date(r.started_at)||!date(r.completed_at)||!Number.isInteger(r.active_ms)||r.active_ms<=0||wall<=0||r.active_ms>wall+1000)errors.push('请开始并实际进行标注计时');
    if(r.target_polygon!==null&&!shared.polygonValid(r.target_polygon))errors.push('目标轮廓无效');
    if(r.target_confirmed){
      if(!shared.polygonValid(r.target_polygon)||!text(r.target_brief,600,true))errors.push('确认目标需要完整轮廓和道路描述');
      if(!date(r.target_registered_at)||Date.parse(r.target_registered_at)<Date.parse(r.started_at)||Date.parse(r.target_registered_at)>Date.parse(r.completed_at))errors.push('目标确认时间不在本次标注内');
    }else if(r.target_registered_at!==null)errors.push('未确认目标不能保留确认时间');
    if(['normal','obstacle'].includes(r.verdict)&&(!r.target_confirmed||!r.full_target_inspected))errors.push('正常或有障碍必须完整检查已确认目标');
    if(r.verdict==='wrong_target'&&(!text(r.task_reference,600,true)||!text(r.target_brief,600,true)))errors.push('错目标需要独立任务定义及依据');
    if(!Array.isArray(r.objects)||r.objects.length>100)errors.push('实体证据列表无效');
    else{
      if((r.verdict==='obstacle')!==(r.objects.length>0))errors.push('有障碍须圈出实体；其他结论请移除实体轮廓');
      for(const o of r.objects)if(!exact(o,['defect_type','polygon'])||!kinds.includes(o.defect_type)||!shared.polygonValid(o.polygon))errors.push('实体类型或轮廓无效');
    }
    for(const k of ['asset_id','site_id','capture_batch_id','near_duplicate_group'])if(typeof r[k]!=='string'||(r[k]&&!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$/.test(r[k])))errors.push('分组标识请使用英文字母、数字或 . _ : -，未知可留空');
    if(r.grouping_verified&&(!['asset_id','site_id','capture_batch_id','near_duplicate_group'].every(k=>r[k])||!text(r.grouping_evidence,1000,true)))errors.push('已核对分组需要全部身份和依据');
    if(s?.ai_suggestion){
      if(r.ai_draft_sha256!==s.ai_suggestion_sha256||r.model_outputs_seen!==true||r.ai_original_checked!==true||typeof r.evaluated_model_outputs_seen!=='boolean'||!['accepted','revised','rejected'].includes(r.ai_draft_decision))errors.push('请逐项核对原图、选择草稿处理结果并填写待评模型暴露');
      if(Date.parse(s.ai_suggestion.inspected_at)>Date.parse(r.started_at))errors.push('人工核对不能早于 AI 草稿');
      if((r.ai_draft_decision==='accepted'&&!unchanged(r,s))||(r.ai_draft_decision==='revised'&&unchanged(r,s)))errors.push('草稿处理结果与实际修改不符');
    }
    return [...new Set(errors)];
  }
  function blank(s,qa=false){return {sample_id:s.sample_id,image_sha256:s.image_sha256,revision:1,reviewer_id:'',origin:qa?'qa_fixture':'operator_entered',phase:'annotation',started_at:null,completed_at:null,active_ms:0,target_brief:'',target_registered_at:null,target_polygon:null,target_confirmed:false,full_target_inspected:false,verdict:'',objects:[],notes:'',task_reference:'',model_outputs_seen:s.ai_suggestion?true:null,source_labels_seen:null,metadata_seen:false,asset_id:'',site_id:'',capture_batch_id:'',near_duplicate_group:'',grouping_verified:false,grouping_evidence:'',...(s.ai_suggestion?{ai_draft_sha256:s.ai_suggestion_sha256,ai_draft_decision:'',ai_original_checked:false,evaluated_model_outputs_seen:null}:{})};}
  function applySuggestion(r,s){if(!s.ai_suggestion)throw Error('本图没有 AI 草稿');const next=structuredClone(r);for(const k of ['target_brief','target_polygon','verdict','objects','notes'])next[k]=structuredClone(s.ai_suggestion[k]);return {...next,target_confirmed:false,target_registered_at:null,full_target_inspected:false,model_outputs_seen:true,ai_original_checked:false,ai_draft_decision:'',ai_draft_sha256:s.ai_suggestion_sha256};}
  function restoreDraft(r,s,qa=false){
    const initial=blank(s,qa);
    if(!exact(r,expected(s))||r.sample_id!==s.sample_id||r.image_sha256!==s.image_sha256||r.origin!==initial.origin||r.phase!=='annotation')return initial;
    if(!['reviewer_id','target_brief','notes','task_reference','asset_id','site_id','capture_batch_id','near_duplicate_group','grouping_evidence'].every(k=>typeof r[k]==='string'))return initial;
    if(!Number.isInteger(r.active_ms)||r.active_ms<0||!Array.isArray(r.objects)||r.objects.length>100||r.objects.some(o=>!exact(o,['defect_type','polygon'])||!kinds.includes(o.defect_type)||!shared.polygonValid(o.polygon)))return initial;
    if(r.target_polygon!==null&&!shared.polygonValid(r.target_polygon))return initial;
    if(!['target_confirmed','full_target_inspected','metadata_seen','grouping_verified'].every(k=>typeof r[k]==='boolean')||!['model_outputs_seen','source_labels_seen'].every(k=>r[k]===null||typeof r[k]==='boolean'))return initial;
    if(s.ai_suggestion&&(r.ai_draft_sha256!==s.ai_suggestion_sha256||r.model_outputs_seen!==true||typeof r.ai_original_checked!=='boolean'||!['','accepted','revised','rejected'].includes(r.ai_draft_decision)||!(r.evaluated_model_outputs_seen===null||typeof r.evaluated_model_outputs_seen==='boolean')))return initial;
    return structuredClone(r);
  }
  function makeExport(bundle,records){
    if(!Array.isArray(records)||records.length>bundle.samples.length)throw Error('记录数量无效');
    const seen=new Set();
    for(const r of records){const errors=validate(r,bundle.samples.find(s=>s.sample_id===r.sample_id),bundle.qa_fixture);if(seen.has(r.sample_id))errors.push('标注重复');if(errors.length)throw Error(errors.join('；'));seen.add(r.sample_id);}
    return {format:'drone.p6-road-operator-labels/v1',bundle_id:bundle.bundle_id,corpus_id:bundle.corpus_id,qa_fixture:bundle.qa_fixture,records,formal_release_eligible:false,reviewer_identity_authenticated:false};
  }
  function parseImport(source,bundle){
    const v=JSON.parse(source);
    if(!exact(v,['format','bundle_id','corpus_id','qa_fixture','records','formal_release_eligible','reviewer_identity_authenticated'])||v.format!=='drone.p6-road-operator-labels/v1'||v.bundle_id!==bundle.bundle_id||v.corpus_id!==bundle.corpus_id||v.qa_fixture!==bundle.qa_fixture||v.formal_release_eligible!==false||v.reviewer_identity_authenticated!==false)throw Error('导入文件的素材包、来源或权限标记不匹配');
    return makeExport(bundle,v.records);
  }
  const api={validate,blank,restoreDraft,makeExport,parseImport,applySuggestion,unchanged,polygonValid:shared.polygonValid,ActiveClock:shared.ActiveClock};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.RoadLabelCore=api;
})(typeof window==='undefined'?globalThis:window);
