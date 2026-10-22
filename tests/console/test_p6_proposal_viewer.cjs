/* Locator coordinates and filtering never create a model or human conclusion. / 定位坐标与筛选不产生模型或人工结论。 */
const test=require('node:test'),assert=require('node:assert/strict');
const C=require('../../scripts/p6_proposal_viewer/core.js');
test('same normalized window maps to both native and model frames',()=>{
  const box=[.699,.075,.732,.097],a=C.rect(box,[4000,3000]),b=C.rect(box,[1344,1008]);
  for(let i=0;i<4;i++)assert.ok(Math.abs(a[i]*.336-b[i])<1e-9);
  assert.equal(a[0],2796);assert.equal(a[1],225);
});
test('tiny proposal windows retain exact dimensions',()=>{
  const r=C.rect([.2,.2,.200001,.200002],[4000,3000]);
  assert.ok(r[2]>0&&r[2]<.01);assert.ok(r[3]>0&&r[3]<.01);
});
test('context and close focus contain the selected window at all image edges',()=>{
  for(const box of [[0,0,.02,.03],[.95,.94,1,1],[.2,.3,.7,.8]])for(const tight of [true,false]){
    const [x,y,w,h]=C.focus(box,[4000,3000],tight),[bx,by,bw,bh]=C.rect(box,[4000,3000]);
    assert.ok(x>=0&&y>=0&&x+w<=4000&&y+h<=3000);
    assert.ok(x<=bx&&y<=by&&x+w>=bx+bw&&y+h>=by+bh);
  }
});
test('repeated zoom and pan stay finite and inside the source frame',()=>{
  let view=C.focus([.1,.2,.3,.4],[4000,3000]);
  for(let i=0;i<100;i++)view=C.zoom(view,[4000,3000],.5,[view[0],view[1]]);
  assert.ok(view.every(Number.isFinite)&&view[2]>=4000/150&&view[3]>=3000/150);
  assert.deepEqual(C.bounded([-10000,99999,400,300],[4000,3000]),[0,2700,400,300]);
  for(let i=0;i<100;i++)view=C.zoom(view,[4000,3000],2);
  assert.deepEqual(view,[0,0,4000,3000]);
});
test('pointer zoom preserves an interior anchor position',()=>{
  const view=[1000,800,1000,750],anchor=[1250,1000],next=C.zoom(view,[4000,3000],.5,anchor);
  assert.equal((anchor[0]-view[0])/view[2],(anchor[0]-next[0])/next[2]);
  assert.equal((anchor[1]-view[1])/view[3],(anchor[1]-next[1])/next[3]);
});
test('filtering preserves all underlying evidence and can have no match',()=>{
  const rows=[{proposal_id:'a',view_id:'one',priority:'first',human_verdict:null,review_reasons:[]},{proposal_id:'b',view_id:'two',priority:'remaining',human_verdict:null,review_reasons:[]}];
  const before=JSON.stringify(rows);
  assert.equal(C.filtered(rows,'',false).length,2);assert.equal(C.filtered(rows,'',true).length,1);
  assert.deepEqual(C.filtered(rows,'two',true),[]);assert.equal(C.initial([]),null);
  assert.equal(JSON.stringify(rows),before);
});
test('initial selection locates the recorded spatial contradiction',()=>{
  const a={review_reasons:['unconfirmed_model_group']},b={review_reasons:['candidate_window_disjoint_from_provisional_possible_road']};
  assert.equal(C.initial([a,b]),b);
});
