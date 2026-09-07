/* derate mockup. Split so the file boundaries match the React port's
   package layout (Part G): what lives here lands in shell/App + shell/Header.
   Classic scripts, loaded in order -- NOT ES modules, because every
   generated onclick= in this file resolves against global scope and
   module scope would break all twelve of them. */
// ---------- chrome ----------
document.querySelectorAll('.dest button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.dest button').forEach(x=>x.setAttribute('aria-selected',x===b));
  ['cluster','dash','spend','settings'].forEach(v=>$('#d-'+v).hidden=(v!==b.dataset.d));
  if(b.dataset.d==='spend') spend();
  if(b.dataset.d==='settings') settings();
  if(b.dataset.d==='dash'){ depsStrip(); aggregate(); loadMatrix(); }
  if(b.dataset.d==='cluster') fitGraph();
});
$('#tog').onclick=e=>{ const hid=$('#wrap').classList.toggle('narrow'), b=e.currentTarget;
  b.setAttribute('aria-expanded',String(!hid));
  b.setAttribute('aria-label',hid?'Show sidebar':'Hide sidebar'); b.title=b.getAttribute('aria-label');
  setTimeout(fitGraph,200); };
$('#theme').onclick=e=>{ const d=document.documentElement.getAttribute('data-theme')==='dark';
  document.documentElement.setAttribute('data-theme',d?'light':'dark'); e.target.textContent=d?'Dark':'Light'; };
$('#whyBtn').onclick=e=>{ const on=$('#whyBox').classList.toggle('on'); e.target.textContent=on?'why ▾':'why ▸'; };


// Boot. Everything above is declarations; this is the only place that runs them.
roster(); sidebar(); depsStrip(); aggregate(); fitGraph(); spend(); settings(); loadMatrix(); rail();

setInterval(()=>{
  S.deps.forEach((d,i)=>{
    const sc=d.span?(S.nodes[d.span[0]].thr+S.nodes[d.span[1]].thr)/200:S.nodes[d.solo.n].thr/100;
    const h=S.hist[d.name]; if(!h) return;
    // Scale once, at the source, so the strip cell, the hero total and the
    // telemetry charts cannot disagree about the same quantity.
    const prev=h[h.length-1]/(h.sc||1);
    const base=Math.max(0,Math.min(prev+(Math.random()-.5)*8, d.tps*1.15)*(0.98+0.04*Math.random()));
    const v=base*sc; h.sc=sc; h.push(v); h.shift();
    const cell=document.getElementById('dt'+i);
    if(cell) cell.innerHTML=`${v.toFixed(0)} <span class="unit">tok/s</span>`;
  });
  document.querySelectorAll('#graph [data-nf]').forEach(e=>{
    const n=S.nodes[+e.getAttribute('data-nf')];
    if(n) e.textContent=`${(n.tps*n.thr/100).toFixed(0)} tok/s · ${Math.round(n.w*n.thr/100)} W`; });
  if(S.openDep!=null){
    const d=S.deps[S.openDep], h=d&&S.hist[d.name];
    if(h){ const v=h[h.length-1], set=(id,x)=>{const e=document.getElementById(id); if(e) e.textContent=x;};
      set('md-tps',fmtN(v)); set('md-dtps',fmtN(d.dtps));
      set('md-itl',fmtN(d.dtps>0?1000/d.dtps:null,1));
      set('md-ttft',fmtN(d.ttft)); set('md-q',d.queue); }
  }
  pushTel(); telemetry();
  S.spentToday+=S.cloudOff?0.0002:0.0006;
  if(!$('#d-spend').hidden) spend();
  if(!$('#d-dash').hidden){ aggregate(); if(Math.random()<0.25) depsStrip(); }
},1000);
