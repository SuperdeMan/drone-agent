/* Coordinate math is independent of image resolution and model judgment. / 坐标计算独立于图像分辨率和模型判断。 */
(function(root){
  'use strict';
  const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
  function rect(box,size){return [box[0]*size[0],box[1]*size[1],(box[2]-box[0])*size[0],(box[3]-box[1])*size[1]];}
  function bounded(view,size){
    const w=clamp(view[2],size[0]/150,size[0]),h=clamp(view[3],size[1]/150,size[1]);
    return [clamp(view[0],0,size[0]-w),clamp(view[1],0,size[1]-h),w,h];
  }
  function focus(box,size,tight=false){
    const f=Math.min(1,Math.max((box[2]-box[0])*(tight?1.5:3),(box[3]-box[1])*(tight?1.5:3),tight?.025:.14));
    return bounded([((box[0]+box[2])/2-f/2)*size[0],((box[1]+box[3])/2-f/2)*size[1],size[0]*f,size[1]*f],size);
  }
  function zoom(view,size,factor,anchor){
    const px=anchor?clamp((anchor[0]-view[0])/view[2],0,1):.5,py=anchor?clamp((anchor[1]-view[1])/view[3],0,1):.5;
    const w=clamp(view[2]*factor,size[0]/150,size[0]),h=clamp(view[3]*factor,size[1]/150,size[1]);
    return bounded([view[0]+px*(view[2]-w),view[1]+py*(view[3]-h),w,h],size);
  }
  function filtered(items,scope,priority){return items.filter(r=>(!scope||r.view_id===scope)&&(!priority||r.priority==='first'));}
  function initial(items){return items.find(r=>r.review_reasons.includes('candidate_window_disjoint_from_provisional_possible_road'))||items[0]||null;}
  const api={rect,bounded,focus,zoom,filtered,initial};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.ProposalCore=api;
})(typeof window==='undefined'?globalThis:window);
