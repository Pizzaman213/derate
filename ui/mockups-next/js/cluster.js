/* derate mockup. Split so the file boundaries match the React port's
   package layout (Part G): what lives here lands in tabs/Cluster + flow/.
   Classic scripts, loaded in order -- NOT ES modules, because every
   generated onclick= in this file resolves against global scope and
   module scope would break all twelve of them. */
// ---------- graph ----------
const paths={};
let GW=700;
// The viewBox used to track clientWidth 1:1, so every font-size in the graph was
// its literal pixel size -- and the graph is authored at 9px, well under the 12px
// floor the rest of the panel keeps. Dividing the coordinate space scales every
// glyph, stroke and gap together, so not one layout constant has to move.
const GSCALE=12/9;
// Pan and zoom move the VIEWPORT, never the layout. Node positions stay a pure
// function of the data, so a machine never moves between refreshes. Panning
// only rewrites one transform attribute -- it does not re-render, so in-flight
// particles survive a drag.
const VIEW={k:1,tx:0,ty:0,drag:null};
const clampv=(v,a,b)=>Math.max(a,Math.min(b,v));
function applyView(){
  const sc=document.getElementById('scene');
  if(sc) sc.setAttribute('transform',
    `translate(${VIEW.tx.toFixed(1)} ${VIEW.ty.toFixed(1)}) scale(${VIEW.k.toFixed(3)})`);
  const z=document.getElementById('zoomLbl');
  if(z) z.textContent=Math.round(VIEW.k*100)+'%';
}
function resetView(){ VIEW.k=1; VIEW.tx=0; VIEW.ty=0; applyView(); }
function zoomAt(px,py,f){
  const nk=clampv(VIEW.k*f,0.4,4), r=nk/VIEW.k;
  VIEW.tx=px-(px-VIEW.tx)*r; VIEW.ty=py-(py-VIEW.ty)*r; VIEW.k=nk; applyView();
}
function render(){
  const g=$('#graph'); g.innerHTML=''; for(const k in paths) delete paths[k];
  const NX=152,NW=134,TX=340,TW=Math.max(240,GW-TX);
  // Two gutters, so a vertical run belongs to exactly one class of edge.
  // G1 carries measured, on-premises paths. G2 carries everything that crosses
  // the routing boundary, where there is no measured fabric to draw a figure
  // from -- so those edges are thin and dashed, the same grammar an unmeasured
  // node-to-node link already uses. Weight is not opacity: opacity means idle.
  const G1=300,G2=314,G3=328;
  const OFFBOX={fill:'none',stroke:'var(--ink)','stroke-width':2.5,
    'stroke-dasharray':'5 4','stroke-linecap':'butt'};
  const junction=(x,y,o)=>el('circle',{cx:x,cy:y,r:3,fill:'var(--ink)',opacity:o});
  const conns=[],boxes=[],rows=[]; let y=36;

  S.deps.forEach((d,di)=>{
    let bf=[];
    // Compact always, expanded on select. Height is data-driven so the
    // selected node grows in place rather than opening a modal over the graph.
    if(d.span){ const bw=Math.round((TW-64)/2);
      const h=d.span.includes(S.selNode)?100:70;
      bf=[{x:TX,y,w:bw,h,n:d.span[0]},{x:TX+bw+64,y,w:bw,h,n:d.span[1]}]; y+=h+28; }
    else if(!S.nodes[d.solo.n].remote){
      const h=d.solo.n===S.selNode?84:54;
      bf=[{x:TX,y,w:TW,h,n:d.solo.n,g:d.solo.g}]; y+=h+10; }
    rows.push({d,di,local:bf,ny:bf.length?bf[0].y+bf[0].h/2:null});
  });
  const placed=rows.filter(r=>r.ny!=null);
  const bd=y+14; let ry=bd+20;
  const remotes=[];
  S.nodes.forEach((n,ni)=>{ if(n.remote){ remotes.push({n:ni,x:TX,y:ry,w:TW,h:38}); ry+=46; } });
  rows.forEach(r=>{ if(r.ny==null){ const rb=remotes.find(x=>x.n===r.d.solo.n); if(rb) r.ny=rb.y+rb.h/2; } });
  const provY=ry; if(S.providers.length&&!S.cloudOff) ry+=46;
  g.setAttribute('viewBox',`0 0 ${GW} ${ry+14}`);

  boxes.push(el('text',{x:0,y:9,'font-size':11,fill:'var(--muted)'},'endpoint'));
  boxes.push(el('text',{x:NX,y:9,'font-size':11,fill:'var(--muted)'},'served names'));
  boxes.push(el('text',{x:TX,y:9,'font-size':11,fill:'var(--muted)'},'targets · measured, plans possible'));

  const ey=placed.length?placed[Math.floor(placed.length/2)].ny:120;
  boxes.push(el('rect',{x:0,y:ey-23,width:132,height:46,rx:4,fill:'var(--fill)'}));
  boxes.push(el('text',{x:12,y:ey-3,class:'m','font-size':10,fill:'var(--onfill)'},'POST /v1/chat/'));
  boxes.push(el('text',{x:12,y:ey+12,class:'m','font-size':10,fill:'var(--onfill)'},'completions'));

  rows.forEach(r=>{
    if(r.ny==null) return;
    conns.push(el('path',{d:`M132 ${ey} H146 V${r.ny} H${NX}`,fill:'none',stroke:'var(--ink)','stroke-width':5,'stroke-linecap':'square'}));
    const ng=el('g',{class:'name','data-d':r.di});
    ng.appendChild(el('rect',{x:NX,y:r.ny-18,width:NW,height:36,rx:3,fill:'var(--fill)',
      ...(r.di===S.sel?{stroke:'var(--ink)','stroke-width':2}:{})}));
    ng.appendChild(el('text',{x:NX+11,y:r.ny+4,class:'m','font-size':10,fill:'var(--onfill)'},r.d.name));
    boxes.push(ng);

    r.local.forEach((b,i)=>{
      const my=b.y+b.h/2;
      conns.push(el('path',{d:`M${NX+NW} ${r.ny} H${G1} V${my} H${b.x}`,fill:'none',stroke:'var(--ink)','stroke-width':5,'stroke-linecap':'square'}));
      paths[r.di+'#L'+i]=[{x:132,y:ey},{x:146,y:ey},{x:146,y:r.ny},{x:NX+NW,y:r.ny},{x:G1,y:r.ny},{x:G1,y:my},{x:b.x+b.w/2,y:my}];
      const n=S.nodes[b.n], selD=b.n===S.selNode;
      const grp=el('g',{class:'node','data-n':b.n});
      if(selD) grp.appendChild(el('rect',{x:b.x-3,y:b.y-3,width:b.w+6,height:b.h+6,rx:6,
        fill:'none',stroke:'var(--ink)','stroke-width':1.5}));
      grp.appendChild(el('rect',{x:b.x,y:b.y,width:b.w,height:b.h,rx:4,fill:'var(--fill)'}));
      const sl=b.g!=null?n.slots[b.g]:n.slots[0];
      grp.appendChild(el('text',{x:b.x+11,y:b.y+15,class:'m','font-size':9,fill:'var(--onfill)'},
        b.g!=null?`${n.id} · gpu${b.g}`:n.id));
      grp.appendChild(el('rect',{x:b.x+11,y:b.y+21,width:b.w-22,height:14,rx:2,fill:'var(--onfill)',opacity:.18}));
      grp.appendChild(el('rect',{x:b.x+11,y:b.y+21,width:(b.w-22)*(sl?sl.pc/100:0),height:14,rx:2,fill:'var(--onfill)',opacity:.85}));
      // Was --fill text sitting on the bar: legible over the .85 fill, but 1.65:1
      // over the .18 track behind it -- so it vanished whenever the bar was short
      // or the slot read 'free'. The bar is geometry; the name belongs on the id row.
      grp.appendChild(el('text',{x:b.x+b.w-11,y:b.y+15,class:'m','font-size':9,
        fill:'var(--onfill)','text-anchor':'end'},sl?sl.m:'free'));
      // Always, not only on tall PP boxes: the same node used to show telemetry
      // as half a pipeline and hide it when solo.
      const tl=el('text',{x:b.x+11,y:b.y+47,class:'m','font-size':9,
        fill:'var(--onfill-dim)','data-nf':b.n},'');
      grp.appendChild(tl);
      if(selD){
        // Only these four refresh at 1 Hz from the metrics frame. Everything
        // below is fetch-time and must not be made to look live.
        grp.appendChild(el('text',{x:b.x+11,y:b.y+61,class:'m','font-size':9,
          fill:'var(--onfill-dim)'},`${n.t} °C · GPU ${n.cpu}% · ${n.bw} GB/s`));
        grp.appendChild(el('text',{x:b.x+11,y:b.y+74,class:'m','font-size':9,
          fill:'var(--onfill-dim)'},`${n.gpu}${n.unified?' · unified memory':''}`));
      }
      boxes.push(grp);
    });

    if(r.d.span){
      const a=r.local[0],b=r.local[1],mid=a.x+a.w,gap=b.x-mid;
      // No bytes-on-the-wire telemetry exists anywhere in the control plane, so
      // "74% utilised" measured nothing. all_reduce_gbps is a one-off saturation
      // figure; the only honest proportion is against the 40 GB/s TP threshold.
      // The bracket is the one node-to-node link a deployment spans, so it is
      // also the way into that link's annotation.
      const li=S.links.findIndex(L=>(L.a===a.n&&L.b===b.n)||(L.a===b.n&&L.b===a.n));
      const lk=li>=0?S.links[li]:null;
      const frac=lk&&lk.measured?Math.min(1,lk.ar/40):0;
      const bg=el('g',{class:'name'});
      if(li>=0) bg.setAttribute('data-link',li);
      bg.appendChild(el('rect',{x:mid,y:a.y+14,width:gap,height:18,fill:'var(--panel)',opacity:0}));
      bg.appendChild(el('rect',{x:mid,y:a.y+19,width:gap,height:7,fill:'var(--ink)',opacity:.16,
        ...(lk&&!lk.measured?{stroke:'var(--ink)','stroke-width':1,'stroke-dasharray':'3 3'}:{})}));
      if(frac>0) bg.appendChild(el('rect',{x:mid,y:a.y+19,width:gap*frac,height:7,fill:'var(--ink)'}));
      boxes.push(bg);
      // Both captions are wider than the 64px gap they sit in, so they cross the
      // node boxes -- which are filled --fill, the same value as --ink. Without a
      // knockout that is ink on ink at 1:1. Plate first, text second.
      // The caption is wider than the 64px gap, so on the box row it crossed both
      // node boxes -- filled --fill, the same value as --ink. It now sits in the
      // gutter above the row, over --panel, with a knockout as insurance.
      // An unmeasured pair carries NO figure. Architecture rule, not styling.
      const LB=lk&&lk.measured?`${lk.ar} of 40 GB/s`:'never measured', lbw=LB.length*5.4+10;
      boxes.push(el('rect',{x:mid+gap/2-lbw/2,y:a.y-17,width:lbw,height:13,rx:2,fill:'var(--panel)'}));
      boxes.push(el('text',{x:mid+gap/2,y:a.y-8,class:'m','font-size':9,fill:'var(--ink)',
        'text-anchor':'middle'},LB));
      // This one is narrower than the gap, so it needs no plate -- and --muted now
      // clears AA on --panel in both themes.
      boxes.push(el('text',{x:mid+gap/2,y:a.y+62,'font-size':9,fill:'var(--muted)',
        'text-anchor':'middle'},'TP 2 rejected'));
    }
  });

  const BLAB='routing boundary · managed, never sharded';
  boxes.push(el('line',{x1:0,y1:bd,x2:GW,y2:bd,stroke:'var(--rule)'}));
  boxes.push(el('rect',{x:TX-4,y:bd-9,width:BLAB.length*5.3+8,height:18,fill:'var(--panel)'}));
  boxes.push(el('text',{x:TX,y:bd+4,'font-size':11,fill:'var(--muted)'},BLAB));

  const w=curW();
  remotes.forEach(rb=>{
    const n=S.nodes[rb.n];
    const owner=rows.find(r=>tgts(r.d).some(x=>x.n===rb.n));
    const ti=owner?tgts(owner.d).findIndex(x=>x.n===rb.n):-1;
    const act=owner&&owner.di===S.sel&&ti>=0&&w[ti]>0&&!S.managedOff;
    if(owner&&owner.ny!=null&&!S.managedOff){
      conns.push(el('path',{...OFFBOX,d:`M${NX+NW} ${owner.ny} H${G2} V${rb.y+rb.h/2} H${rb.x}`,
        opacity:act?1:.5}));
      conns.push(junction(G2,owner.ny,act?1:.5));
      paths[owner.di+'#R'+rb.n]=[{x:132,y:ey},{x:146,y:ey},{x:146,y:owner.ny},{x:NX+NW,y:owner.ny},
        {x:G2,y:owner.ny},{x:G2,y:rb.y+rb.h/2},{x:rb.x+rb.w/2,y:rb.y+rb.h/2}];
    }
    // Opacity on the group composited the labels too, which put the excluded-node
    // text at 2.0:1. Idle keeps its opacity (that still clears AA); excluded drops
    // to an outline instead -- the same grammar the third-party provider box uses.
    const off=S.managedOff, rtxt=off?'var(--muted)':'var(--onfill)';
    const grp=el('g',{class:'node','data-n':rb.n,...(off?{}:{opacity:act?1:.62})});
    grp.appendChild(el('rect',{x:rb.x,y:rb.y,width:rb.w,height:rb.h,rx:4,
      ...(off?{fill:'none',stroke:'var(--rule)','stroke-width':1}:{fill:'var(--fill)'})}));
    grp.appendChild(el('text',{x:rb.x+11,y:rb.y+15,class:'m','font-size':9,fill:rtxt},
      `${n.id} · 1 slot · ${n.lat} ms · $${(n.rate||0).toFixed(2)}/hr${S.managedOff?' · excluded':''}`));
    grp.appendChild(el('rect',{x:rb.x+11,y:rb.y+21,width:rb.w-22,height:14,rx:2,
      fill:off?'var(--rule)':'var(--onfill)',opacity:off?.5:.18}));
    grp.appendChild(el('rect',{x:rb.x+11,y:rb.y+21,width:(rb.w-22)*(n.slots[0]?n.slots[0].pc/100:0),height:14,rx:2,
      fill:off?'var(--muted)':'var(--onfill)',opacity:off?.55:.85}));
    grp.appendChild(el('text',{x:rb.x+rb.w-11,y:rb.y+15,class:'m','font-size':9,
      fill:rtxt,'text-anchor':'end'},n.slots[0]?n.slots[0].m:'free'));
    boxes.push(grp);
  });

  // A provider is ONE endpoint that can serve anything, so it is drawn as a bus:
  // a rail tapping every running model, and a single trunk into the box. N edges
  // converging on a point states the same relation N times. A box per remote
  // model would put ~300 nodes in this column and is also the model-catalog
  // browser we are told not to build.
  // Tap weight is the honest part: 2.5px means the provider is a configured
  // target for that name and will take traffic; 1px hairline means merely
  // reachable through the proxy. Drawing those identically would claim routing
  // that is not configured.
  const P=S.providers[0];
  if(P&&S.providers.length){
    if(!S.cloudOff){
      const named=rows.filter(r=>r.ny!=null);
      let anyAct=false;
      if(named.length){
        const top=Math.min(...named.map(r=>r.ny));
        // one rail, one entry into the provider
        conns.push(el('path',{...OFFBOX,d:`M${G3} ${top} V${provY+19} H${TX}`,opacity:.5}));
        named.forEach(o=>{
          const cfg=P.spill.includes(o.d.name);
          const t=tgts(o.d), pi=t.findIndex(x=>x.kind==='prov');
          const act=cfg&&o.di===S.sel&&pi>=0&&w[pi]>0; anyAct=anyAct||act;
          conns.push(el('path',{...OFFBOX,d:`M${NX+NW} ${o.ny} H${G3}`,
            'stroke-width':cfg?2.5:1,opacity:act?1:(cfg?.5:.22)}));
          if(cfg) conns.push(junction(G3,o.ny,act?1:.5));
          if(cfg) paths[o.di+'#P']=[{x:132,y:ey},{x:146,y:ey},{x:146,y:o.ny},{x:NX+NW,y:o.ny},
            {x:G3,y:o.ny},{x:G3,y:provY+19},{x:TX+TW/2,y:provY+19}];
        });
      }
      // Idle reads on the outline. The words carry their own colour, because a
      // .55 group put this box's primary line at 3.70:1 in light mode.
      const grp=el('g',{});
      grp.appendChild(el('rect',{x:TX+.5,y:provY+.5,width:TW-1,height:38,rx:3,fill:'none',
        stroke:'var(--ink)','stroke-width':1,opacity:anyAct?1:.55}));
      grp.appendChild(el('text',{x:TX+11,y:provY+15,class:'m','font-size':9,
        fill:anyAct?'var(--ink)':'var(--muted)'},
        `${P.id} · third party · $${P.cost.toFixed(3)}/Mtok · no telemetry`));
      grp.appendChild(el('text',{x:TX+11,y:provY+30,class:'m','font-size':9,fill:'var(--muted)'},
        `proxy for any served name · ${P.models} models · ${
          P.spill.length?`routed for ${P.spill.join(', ')}`:'none routed here yet'}`));
      boxes.push(grp);
    } else {
      boxes.push(el('text',{x:TX,y:provY+20,'font-size':11,fill:'var(--muted)'},
        'cloud providers disabled · local only'));
    }
  }

  const scene=el('g',{id:'scene'});
  conns.forEach(c=>scene.appendChild(c));
  scene.appendChild(el('g',{id:'particles'}));
  boxes.forEach(b=>scene.appendChild(b));
  g.appendChild(scene);
  applyView();
  document.querySelectorAll('#graph .node').forEach(x=>{
    x.addEventListener('click',()=>selNode(+x.dataset.n));
    x.addEventListener('dblclick',()=>inspect(+x.dataset.n));});
  document.querySelectorAll('#graph .name').forEach(x=>{
    if(x.dataset.link!=null){ x.addEventListener('click',()=>selLink(+x.dataset.link)); return; }
    x.addEventListener('click',()=>selDep(+x.dataset.d));
    x.addEventListener('dblclick',()=>inspectDep(+x.dataset.d));});
}

function fitGraph(){
  const g=$('#graph'); if(!g) return;
  // The 640 floor is a *rendered* minimum, so it has to be divided by GSCALE too;
  // left at 640 it out-clamps the scale below ~900px and the type falls back under 12px.
  GW=Math.max(Math.round(640/GSCALE),Math.round((g.clientWidth||700)/GSCALE));
  render();
}
let _rz=0;
addEventListener('resize',()=>{ if(!_rz) _rz=requestAnimationFrame(()=>{_rz=0;fitGraph();}); });

(function(){
  const g=$('#graph'); if(!g) return;
  const toVB=e=>{ const r=g.getBoundingClientRect(), vb=g.viewBox.baseVal;
    return {x:(e.clientX-r.left)/r.width*(vb.width||GW),
            y:(e.clientY-r.top)/r.height*(vb.height||400),
            sx:(vb.width||GW)/r.width, sy:(vb.height||400)/r.height}; };
  g.style.cursor='grab'; g.style.touchAction='none';
  g.setAttribute('tabindex','0');
  g.setAttribute('aria-label','Request flow graph. Drag to pan, scroll to zoom. Arrow keys pan, plus and minus zoom, 0 resets.');

  g.addEventListener('wheel',e=>{ e.preventDefault(); const p=toVB(e);
    zoomAt(p.x,p.y,Math.exp(-e.deltaY*0.0015)); },{passive:false});

  g.addEventListener('pointerdown',e=>{
    if(e.target.closest('.node,.name')) return;
    VIEW.drag={x:e.clientX,y:e.clientY,tx:VIEW.tx,ty:VIEW.ty};
    g.setPointerCapture(e.pointerId); g.style.cursor='grabbing';
  });
  g.addEventListener('pointermove',e=>{
    if(!VIEW.drag) return; const p=toVB(e);
    VIEW.tx=VIEW.drag.tx+(e.clientX-VIEW.drag.x)*p.sx;
    VIEW.ty=VIEW.drag.ty+(e.clientY-VIEW.drag.y)*p.sy;
    applyView();
  });
  const end=()=>{ VIEW.drag=null; g.style.cursor='grab'; };
  g.addEventListener('pointerup',end); g.addEventListener('pointercancel',end);

  // a drag-only viewport has no keyboard path; these give it one
  g.addEventListener('keydown',e=>{
    const step=e.shiftKey?60:20, vb=g.viewBox.baseVal;
    const c={x:(vb.width||GW)/2,y:(vb.height||400)/2};
    const k={ArrowLeft:[step,0],ArrowRight:[-step,0],ArrowUp:[0,step],ArrowDown:[0,-step]}[e.key];
    if(k){ e.preventDefault(); VIEW.tx+=k[0]; VIEW.ty+=k[1]; applyView(); }
    else if(e.key==='+'||e.key==='='){ e.preventDefault(); zoomAt(c.x,c.y,1.2); }
    else if(e.key==='-'||e.key==='_'){ e.preventDefault(); zoomAt(c.x,c.y,1/1.2); }
    else if(e.key==='0'){ e.preventDefault(); resetView(); }
    // Selection was mouse-only; the graph already takes focus for panning.
    else if(e.key==='['||e.key===']'){
      e.preventDefault(); const n=S.links.length; if(!n) return;
      const cur=S.selLink==null?-1:S.selLink;
      selLink(((e.key===']'?cur+1:cur-1)%n+n)%n);
    }
  });
})();

// ---------- selection rail ----------
// The Cluster tab had exactly two text elements outside the SVG. Everything
// below is already-computed data the control plane throws away at the HTTP
// boundary; none of it needs a new measurement.
function rail(){
  const r=document.getElementById('rail'); if(!r) return;
  const row=(k,v)=>`<div class="row"><span>${k}</span><span class="mono">${v}</span></div>`;

  if(S.selLink!=null && S.links[S.selLink]){
    const l=S.links[S.selLink], A=S.nodes[l.a].id, B=S.nodes[l.b].id;
    if(!l.measured){
      r.innerHTML=`<div class="sub" style="border:none;padding-top:0;margin-top:0">link \u00b7 ${A} \u2194 ${B}</div>
        <div class="unit">Never measured. No bandwidth figure is shown because none exists \u2014
        the architecture's rule is that an unmeasured pair carries no numbers at all.</div>
        <button style="margin-top:8px" onclick="alert('Would run nccl-tests between '+'${A}'+' and '+'${B}'+'. This saturates the link for about a minute.')">Measure this link</button>`;
      return;
    }
    // raw -> scaled is only meaningful where a scale was applied. On the TCP
    // rung raw IS the reported figure and scale_factor is null, so rendering an
    // arrow there would be a lie. Gate on scale, never on `estimated`.
    const prov = l.estimated
      ? `<div class="row"><span>Derived</span><span class="mono">${l.raw} GB/s raw \u00d7 ${l.scale} \u2192 ${l.ar} GB/s</span></div>`
      : `<div class="row"><span>Source</span><span class="mono">measured directly</span></div>`;
    r.innerHTML=`<div class="sub" style="border:none;padding-top:0;margin-top:0">link \u00b7 ${A} \u2194 ${B}</div>
      <div class="chartgrid" style="grid-template-columns:1fr 1fr">
        <div>
          ${row('All-reduce', l.ar+' GB/s')}
          ${row('Send/recv', l.sr+' GB/s')}
          ${row('Latency', l.lat+' \u00b5s')}
          ${row('Method', l.method)}
          ${prov}
        </div>
        <div>
          ${row('Estimated', l.estimated?'yes \u2014 scaled, not observed':'no')}
          ${row('GPUDirect RDMA', l.gdr?'enabled':'disabled')}
          ${row('QSFP cages up', l.ports[0]+' of '+l.ports[1]+' \u00b7 read on '+l.portsOn)}
          ${row('Probe took', l.dur+' s')}
          ${row('Measured', l.age)}
        </div>
      </div>
      <div class="unit" style="margin-top:8px">${l.gdrBy}</div>
      <div class="why on" style="margin-top:8px">${l.notes.map(n=>`<div>${n}</div>`).join('')}</div>
      <div class="unit" style="margin-top:8px">${l.ar>=40
        ? 'At or above the 40 GB/s threshold, so tensor parallel is viable across this pair.'
        : 'Below the 40 GB/s tensor-parallel threshold, which is why the planner chooses pipeline parallel over this pair.'}</div>`;
    return;
  }

  if(S.selNode!=null && S.nodes[S.selNode]){
    const n=S.nodes[S.selNode];
    // memory_report() separates the model's pool from the desktop's. On GB10
    // the gap between pool_used and gpu_used IS the operating system.
    const pool=Math.round(128*(n.slots.find(x=>x)?.pc||0)/100);
    r.innerHTML=`<div class="sub" style="border:none;padding-top:0;margin-top:0">${n.id}</div>
      <div class="chartgrid" style="grid-template-columns:1fr 1fr 1fr">
        <div>${row('Hardware',n.gpu)}${row('Memory bandwidth',n.bw+' GB/s')}${row('Class',n.unified?'gb10 \u00b7 unified':'discrete')}</div>
        <div>${row('Power',Math.round(n.w*n.thr/100)+' W')}${row('Temperature',n.t+' \u00b0C')}${row('GPU utilisation',n.cpu+' %')}</div>
        <div>${row('Addressable',n.unified?'119.7 GiB':'22.0 GiB')}${row('Pool used',pool+' GiB')}${row('Allocatable',n.unified?(119.7-pool).toFixed(1)+' GiB':'\u2014')}</div>
      </div>
      <div class="unit" style="margin-top:8px">${n.unified
        ? 'Unified memory: the model and the operating system share one pool, so the static ceiling overstates what is actually allocatable. Only power, temperature, memory and GPU utilisation refresh live; the rest is read at fetch time.'
        : 'Discrete memory. Only power, temperature, memory and GPU utilisation refresh live.'}</div>`;
    return;
  }

  // The graph draws one link -- the pair a deployment spans. /api/links returns
  // EVERY pair, so the rest need a home or "never measured" is unreachable.
  const meas=S.links.filter(l=>l.measured).length;
  r.innerHTML=`<div class="unit" style="margin-bottom:8px">Click a node, or a pair below, for detail. `
    + `${S.links.length} node pairs \u00b7 ${meas} measured \u00b7 ${S.links.length-meas} never measured.</div>`
    + `<div class="chips">` + S.links.map((l,i)=>
        `<button onclick="selLink(${i})">${S.nodes[l.a].id} \u2194 ${S.nodes[l.b].id}`
        + `<span class="unit" style="margin-left:6px">${l.measured?l.ar+' GB/s':'never measured'}</span></button>`
      ).join('') + `</div>`;
}

// ---------- particles ----------
function fly(){
  const t=tgts(dep()), w=curW(); if(!t.length) return;
  let acc=0, idx=0, r=Math.random();
  for(let i=0;i<w.length;i++){ acc+=w[i]; if(r<=acc){ idx=i; break; } }
  const x=t[idx]; if(!x) return;
  let k = x.kind==='prov' ? S.sel+'#P'
    : x.kind==='remote' ? S.sel+'#R'+x.n
    : S.sel+'#L'+(dep().span?(Math.random()<.5?0:1):0);
  const pts=paths[k]; if(!pts) return;
  const layer=$('#particles'); if(!layer) return;
  const box=el('rect',{width:11,height:9,rx:1.5,fill:'var(--flow)'});
  layer.appendChild(box);
  const segs=[]; let tot=0;
  for(let i=1;i<pts.length;i++){ const L=Math.hypot(pts[i].x-pts[i-1].x,pts[i].y-pts[i-1].y); segs.push(L); tot+=L; }
  const dur=1600,t0=performance.now();
  (function step(now){
    const p=Math.min((now-t0)/dur,1); let d=p*tot,i=0;
    while(i<segs.length&&d>segs[i]){ d-=segs[i]; i++; }
    if(i>=segs.length) i=segs.length-1;
    const a=pts[i],b=pts[i+1]||pts[i],f=segs[i]?d/segs[i]:0;
    box.setAttribute('x',a.x+(b.x-a.x)*f-5); box.setAttribute('y',a.y+(b.y-a.y)*f-4);
    if(p<1) requestAnimationFrame(step); else box.remove();
  })(t0);
}
setInterval(()=>{ if(!$('#d-cluster').hidden) fly(); },380);
setInterval(()=>{ if(!$('#d-cluster').hidden&&S.saturated) fly(); },200);

