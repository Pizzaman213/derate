/* derate mockup. Split so the file boundaries match the React port's
   package layout (Part G): what lives here lands in tabs/Dashboard + dashboard/.
   Classic scripts, loaded in order -- NOT ES modules, because every
   generated onclick= in this file resolves against global scope and
   module scope would break all twelve of them. */
// ---------- deployments strip ----------
function depsStrip(){
  $('#deps').innerHTML=S.deps.map((d,i)=>{
    if(!S.hist[d.name]) S.hist[d.name]=Array.from({length:40},()=>d.tps*(0.9+Math.random()*0.2));
    const h=S.hist[d.name], mx=Math.max(...h,1)*1.2;
    const pts=h.map((v,j)=>`${j/39*100},${22-v/mx*20}`).join(' ');
    const t=tgts(d);
    return `<div class="deprow ${i===S.sel?'sel':''}" onclick="selDep(${i})" ondblclick="inspectDep(${i})" title="Double-click for detail">
      <span class="mono" style="font-size:13px">${d.name}</span>
      <span class="unit">${d.plan}</span>
      <svg viewBox="0 0 100 24" preserveAspectRatio="none" style="width:100%;height:24px">
        <polyline points="${pts}" fill="none" stroke="var(--ink)" stroke-width="1" vector-effect="non-scaling-stroke"/></svg>
      <span class="mono" style="text-align:right;font-size:13px" id="dt${i}">${d.tps.toFixed(0)} <span class="unit">tok/s</span></span>
      <span class="unit" style="text-align:right">${t.length} target${t.length===1?'':'s'}
        <button class="ghost dets" style="padding:1px 5px;margin-left:4px"
          onclick="event.stopPropagation();inspectDep(${i})" aria-label="Detail">&#8599;</button></span>
    </div>`;}).join('');
}
function selDep(i){ S.sel=i; sidebar(); depsStrip(); render(); telemetry(); }
S.selNode=null;
S.selLink=null;
function selLink(i){ S.selLink=S.selLink===i?null:i; S.selNode=null; render(); rail(); }
function selNode(i){ S.selNode=S.selNode===i?null:i; S.selLink=null; render(); telemetry(); rail(); }
function aggregate(){
  let tot=0;
  S.deps.forEach(d=>{ const h=S.hist[d.name]; if(h) tot+=h[h.length-1]; });
  const set=(id,v)=>{ const e=document.getElementById(id); if(e) e.textContent=v; };
  set('aTps', tot.toFixed(0));
  set('aDep', S.deps.length);
  set('aPow', Math.round(S.nodes.reduce((a,n)=>a+n.w*n.thr/100,0)));
  set('aFree', S.nodes.reduce((a,n)=>a+n.slots.filter(x=>!x).length,0));
}

// ---------- telemetry history ----------
// Nine series, and only nine, because that is exactly what the 1 Hz frame
// carries: cluster {tokens_per_sec, total_power_w}; per node {power_w, temp_c,
// memory_used_pct, util_pct}; per deployment {tokens_per_sec, ttft_ms,
// queue_depth}. Anything else would be a chart with nothing behind it.
const WINDOW=60;
S.tel={cluster:{tps:[],pw:[]},nodes:{},deps:{}};
function pushTel(){
  const push=(a,v)=>{ a.push(v); if(a.length>WINDOW) a.shift(); };
  let tot=0; S.deps.forEach(d=>{ const h=S.hist[d.name]; if(h) tot+=h[h.length-1]; });
  push(S.tel.cluster.tps, tot);
  push(S.tel.cluster.pw, S.nodes.reduce((a,n)=>a+n.w*n.thr/100,0));
  S.nodes.forEach((n,i)=>{
    const t=S.tel.nodes[i]||(S.tel.nodes[i]={pw:[],temp:[],mem:[],util:[]});
    push(t.pw,n.w*n.thr/100); push(t.temp,n.t); push(t.util,n.cpu);
    const sl=n.slots.find(x=>x); push(t.mem, sl?sl.pc:0);
  });
  S.deps.forEach(d=>{
    const t=S.tel.deps[d.name]||(S.tel.deps[d.name]={tps:[],dtps:[],ttft:[],q:[]});
    const h=S.hist[d.name];
    push(t.tps, h?h[h.length-1]:0);
    push(t.dtps, d.dtps==null?null:d.dtps*(0.94+Math.random()*0.12));
    push(t.ttft, d.ttft==null?null:d.ttft*(0.9+Math.random()*0.2));
    push(t.q, d.queue);
  });
}

// A chart with a visible baseline and its own min/max, so an amplitude is
// readable rather than merely decorative. Null gaps break the line instead of
// drawing across them: a gap is not a value.
function chart(title,unit,data,dp){
  const pts=data.filter(v=>v!=null&&isFinite(v));
  if(!pts.length) return `<div class="chart"><h4><span>${title}</span><span class="now">—</span></h4>
    <div class="unit">no samples yet</div></div>`;
  const mx=Math.max(...pts), mn=Math.min(...pts), span=(mx-mn)||1;
  const W=100,H=34;
  let d='',pen=false;
  data.forEach((v,i)=>{
    const x=(i/Math.max(1,WINDOW-1))*W;
    if(v==null||!isFinite(v)){ pen=false; return; }
    const y=H-((v-mn)/span)*(H-4)-2;
    d+=(pen?'L':'M')+x.toFixed(1)+','+y.toFixed(1)+' '; pen=true;
  });
  const now=pts[pts.length-1];
  return `<div class="chart">
    <h4><span>${title}</span><span class="now">${now.toFixed(dp||0)} <span class="unit">${unit}</span></span></h4>
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:44px;display:block">
      <line x1="0" y1="${H-2}" x2="${W}" y2="${H-2}" stroke="var(--rule)" stroke-width="0.5" vector-effect="non-scaling-stroke"/>
      <path d="${d}" fill="none" stroke="var(--ink)" stroke-width="1" vector-effect="non-scaling-stroke"/></svg>
    <div class="ax"><span>min ${mn.toFixed(dp||0)}</span><span>${pts.length}s</span><span>max ${mx.toFixed(dp||0)}</span></div>
  </div>`;
}

// Drill-down, not a wall. Cluster charts are always visible; per-Spark and
// per-deployment charts render only for the current selection. The wall is 30
// charts at 4 nodes and 3 models but 130 at 12 nodes and 20 models, all
// repainting at 1 Hz, on the surface the density audit flagged as first to
// degrade. Selection is shared with the flow graph (S.selNode) and the
// deployments strip (S.sel), so drilling in one place drills everywhere.
function telemetry(){
  if($('#s-telemetry').hidden) return;
  $('#telCluster').innerHTML =
      chart('Throughput, all models','tok/s',S.tel.cluster.tps,0)
    + chart('Power drawn','W',S.tel.cluster.pw,0);

  const ni = S.selNode==null ? 0 : S.selNode;
  $('#telNodePick').innerHTML = S.nodes.map((n,i)=>
    `<button aria-pressed="${i===ni}" onclick="pickNode(${i})">${n.id}</button>`).join('');
  const n=S.nodes[ni], t=S.tel.nodes[ni]||{pw:[],temp:[],mem:[],util:[]};
  $('#telNodes').innerHTML = n ? (
      chart(n.id+' \u00b7 power','W',t.pw,0)
    + chart(n.id+' \u00b7 temperature','\u00b0C',t.temp,0)
    + chart(n.id+' \u00b7 memory','%',t.mem,0)
    + chart(n.id+' \u00b7 GPU utilisation','%',t.util,0)) : '';

  const d=dep();
  $('#telDepNote').textContent = d
    ? `Showing ${d.name}. Selecting a model anywhere \u2014 the deployments strip or the cluster graph \u2014 changes this.`
    : 'Nothing is being served.';
  const td=d?(S.tel.deps[d.name]||{tps:[],dtps:[],ttft:[],q:[]}):null;
  $('#telDeps').innerHTML = td ? (
      chart(d.name+' \u00b7 aggregate throughput','tok/s',td.tps,0)
    + chart(d.name+' \u00b7 per-stream decode','tok/s',td.dtps,0)
    + chart(d.name+' \u00b7 time to first token','ms',td.ttft,0)
    + chart(d.name+' \u00b7 queued','reqs',td.q,0)) : '';
}
// Picking a Spark here selects it everywhere rather than toggling it off.
function pickNode(i){ S.selNode=i; S.selLink=null; render(); telemetry(); rail(); }

document.querySelectorAll('.subs button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.subs button').forEach(x=>x.setAttribute('aria-selected',x===b));
  ['overview','telemetry','load'].forEach(v=>$('#s-'+v).hidden=(v!==b.dataset.s));
  if(b.dataset.s==='load') loadMatrix();
  if(b.dataset.s==='telemetry') telemetry();
});

// ---------- load by spark ----------
// Derived, not measured: the gateway keys request stats by target_id only
// (StatsRegistry is dict[str, TargetStats]) and has no per-node accounting.
// A deployment's counts are attributed to every node in its plan, because in
// pipeline parallel EVERY request traverses EVERY node -- so a 620-request
// PP-2 model is 620 on each Spark, not 310 and 310. Splitting it would be
// inventing a number. Column totals therefore exceed the request total, and
// the note below says so rather than letting it read as broken arithmetic.
function loadMatrix(){
  const t=document.getElementById('loadTable'); if(!t) return;
  // d.remote holds managed nodes serving the same name; omitting them hid
  // brev-h100 entirely even though it holds a slot and takes traffic.
  const nodesOf=d=>[...(d.span?d.span:(d.solo?[d.solo.n]:[])),...(d.remote||[])];
  const rows=S.nodes.map((n,ni)=>({n,ni,deps:S.deps.filter(d=>nodesOf(d).includes(ni))}))
                    .filter(r=>r.deps.length);
  if(!rows.length){ t.innerHTML=''; $('#loadNote').textContent='Nothing is being served.'; return; }
  let shared=false;
  t.innerHTML=`<tr><th>Spark</th>${S.deps.map(d=>`<th style="text-align:right">${d.name}</th>`).join('')}
      <th style="text-align:right">Node total</th></tr>`
    + rows.map(r=>{
        let tot=0;
        const cells=S.deps.map(d=>{
          if(!nodesOf(d).includes(r.ni)) return '<td class="num mut">—</td>';
          tot+=d.req||0;
          const sh=nodesOf(d).length>1; if(sh) shared=true;
          return `<td class="num">${(d.req||0).toLocaleString()}${
            sh?' <span class="unit">shared</span>':''}</td>`;
        }).join('');
        return `<tr><td class="mono">${r.n.id}</td>${cells}<td class="num">${tot.toLocaleString()}</td></tr>`;
      }).join('');
  const real=S.deps.reduce((a,d)=>a+(d.req||0),0);
  const summed=rows.reduce((a,r)=>a+r.deps.reduce((b,d)=>b+(d.req||0),0),0);
  $('#loadNote').innerHTML = shared
    ? `${real.toLocaleString()} requests today. Node totals sum to ${summed.toLocaleString()} `
      + `because a model spanning Sparks is counted on each — in pipeline parallel every `
      + `request traverses every node, so the count is shared, not divided.`
    : `${real.toLocaleString()} requests today.`;
}

