/* derate mockup. Split so the file boundaries match the React port's
   package layout (Part G): what lives here lands in routing/shares.ts.
   Classic scripts, loaded in order -- NOT ES modules, because every
   generated onclick= in this file resolves against global scope and
   module scope would break all twelve of them. */
// ---------- routing ----------
const NOTES={local_first:'Remote targets take traffic only once every local slot stops admitting.',
  least_outstanding:'Fewest in-flight wins. Accounts for a replica mid-prefill.',
  weighted_capacity:'Share proportional to measured throughput.',
  cost_aware:'Cheapest admitting target. Local priced from measured draw.',
  round_robin:'Even rotation. Ignores that a replica may be mid-prefill.'};
function curW(d){
  const t=tgts(d||dep()), p=$('#policy').value, n=t.length;
  if(n===0) return []; if(n===1) return [1];
  const loc=t.map(x=>x.kind==='local'?1:0), nl=loc.reduce((a,b)=>a+b,0);
  if(p==='local_first'){
    if(!nl) return t.map(()=>1/n);
    return S.saturated&&n>nl ? t.map((x,i)=>loc[i]?0:1/(n-nl)) : t.map((x,i)=>loc[i]?1/nl:0);
  }
  if(p==='cost_aware'){ const c=t.map(x=>x.kind==='prov'?S.providers[0].cost:S.nodes[x.n].cost);
    const m=Math.min(...c); return c.map(v=>v===m?1/c.filter(z=>z===m).length:0); }
  if(p==='weighted_capacity'){ const s=t.map(x=>x.kind==='prov'?0.55:S.nodes[x.n].tps/200);
    const tt=s.reduce((a,b)=>a+b,0); return s.map(v=>v/tt); }
  return t.map(()=>1/n);
}
$('#reset').onclick=resetView;
$('#policy').onchange=()=>{ sidebar(); render(); };
$('#spill').onclick=e=>{ S.saturated=!S.saturated; $('#policy').value='local_first';
  e.target.textContent=S.saturated?'Relieve':'Saturate'; sidebar(); render(); };
$('#measure').onclick=e=>{ e.target.disabled=1; e.target.textContent='Measuring…';
  setTimeout(()=>{ S.link=+(9.8+Math.random()*0.8).toFixed(1); sidebar(); render();
    e.target.disabled=0; e.target.textContent='Re-measure'; },1400); };
$('#splitBtn').onclick=e=>{ S.joined?setSplit():setJoined(); S.sel=0; S.hist={};
  e.target.textContent=S.joined?'Split sparks':'Join sparks'; roster(); sidebar(); depsStrip(); aggregate(); render(); };

