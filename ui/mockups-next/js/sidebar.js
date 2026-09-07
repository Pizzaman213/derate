/* derate mockup. Split so the file boundaries match the React port's
   package layout (Part G): what lives here lands in sidebar/.
   Classic scripts, loaded in order -- NOT ES modules, because every
   generated onclick= in this file resolves against global scope and
   module scope would break all twelve of them. */
// ---------- sidebar ----------
function roster(){
  $('#roster').innerHTML=S.nodes.map((n,i)=>{
    const free=n.slots.filter(s=>!s).length;
    const rows=n.slots.map(s=>s
      ?`<div class="slot"><div class="bar"><i style="width:${s.pc}%"></i></div><span class="mono mut" style="width:100px">${s.m}</span></div>`
      :`<div class="slot"><div class="bar free"></div><span class="mono mut" style="width:100px">free</span></div>`).join('');
    const th=n.thr<100?` · <span style="color:var(--warn)">${n.thr}%</span>`:'';
    const off=n.remote&&S.managedOff;
    return `<div style="margin-bottom:10px;cursor:pointer;${off?'opacity:.45':''}" ondblclick="inspect(${i})">
      <div class="row" style="padding:0 0 3px"><span class="label">${n.id}${n.remote?' <span class="unit">remote</span>':''}</span>
      <span class="mono unit"><span class="dot" style="background:var(--${off?'muted':'live'})"></span> ${Math.round(n.w*n.thr/100)} W${th}</span></div>
      ${rows}<div class="unit" style="margin-top:2px">${free} free · GPU ${n.cpu}%${n.unified?' · shared':''}${n.remote?` · ${n.lat} ms`:''}</div></div>`;
  }).join('');
  const slots=S.nodes.reduce((a,n)=>a+n.slots.length,0), free=S.nodes.reduce((a,n)=>a+n.slots.filter(s=>!s).length,0);
  $('#pill').textContent=`${S.nodes.length} nodes · ${slots} slots · ${free} free`;
  $('#cloudPill').style.display=S.cloudOff?'inline-block':'none';
  $('#place').innerHTML='<option value="auto">auto</option>'+S.nodes.map((n,i)=>`<option value="${i}">${n.id}</option>`).join('');
}

function sidebar(){
  const d=dep(), t=tgts(d), w=curW();
  $('#scopeHead').textContent='Plan · '+d.name;
  $('#planLabel').textContent=d.plan;
  $('#linkNow').textContent=d.span?S.link+' GB/s':'—';
  $('#whyBox').innerHTML=d.span
    ? `<div>Pipeline parallel across 2 nodes, because measured all-reduce is ${S.link} GB/s and target concurrency is 16.</div>
       <div class="mut" style="margin-top:8px">Rejected</div>
       <div>TP 2 — ${S.link} GB/s is below the 40 GB/s threshold.</div>
       <div>Spanning brev-h100 — remote node, no measured fabric. Routing only.</div>`
    : `<div>Fits in one slot on ${S.nodes[d.solo.n].id}, so it is placed whole rather than sharded.</div>
       <div class="mut" style="margin-top:8px">Rejected</div>
       <div>Sharding — would pay a handoff per token to solve a capacity problem that does not exist.</div>`;
  // `weight` is only SEMANTICALLY a traffic share under weighted_capacity and
  // round_robin. Drawing a proportional bar under least_outstanding or failover
  // implies a configured split the router does not use, so those bases get the
  // observed consequence with the rule named instead of a bar.
  const pol=$('#policy').value;
  const BASIS={weighted_capacity:'weight',round_robin:'uniform',
    least_outstanding:'least_outstanding',local_first:'local_then_spill',
    cost_aware:'min_cost'};
  const basis=BASIS[pol]||'least_outstanding';
  const proportional = basis==='weight'||basis==='uniform';
  $('#weights').innerHTML=t.map((x,i)=>{
    const nm=x.kind==='prov'?'openrouter':S.nodes[x.n].id;
    const pctv=(w[i]*100).toFixed(0);
    // A benched target says why. A weight of zero with no reason is the
    // reported symptom this card exists to fix.
    const why = w[i]>0 ? '' :
      (basis==='local_then_spill' && x.kind!=='local' ? 'holds until every local stops admitting'
       : basis==='min_cost' ? 'not the cheapest admitting target'
       : basis==='least_outstanding' ? 'not the shortest queue right now'
       : 'below the 15% strength floor');
    const bar = proportional
      ? `<div class="bar"><i style="width:${pctv}%;opacity:${w[i]?1:.25}"></i></div>`
      : `<div class="bar" style="background:none;box-shadow:inset 0 0 0 1px var(--rule)">
           <i style="width:${pctv}%;opacity:${w[i]?.55:0}"></i></div>`;
    return `<div class="slot">${bar}
      <span class="mono unit" style="width:32px;text-align:right">${pctv}%</span></div>
      <div class="unit" style="margin:-2px 0 6px">${nm}${why?` \u00b7 <span class="mut">${why}</span>`:''}</div>`;
  }).join('');
  $('#policyNote').innerHTML = t.length>1
    ? `${NOTES[pol]}<br><span class="mut">${proportional
        ? 'Bars are the configured share.'
        : 'Bars show where traffic is going right now, not a configured share \u2014 this policy does not set one.'}</span>`
    : 'Single target. Policy has no effect.';
  $('#costHead').textContent='Cost per Mtok · '+d.name;
  // A price with no provenance is a number to distrust. Local cost is derived
  // from measured draw at the configured electricity rate; a provider's is
  // published. And when the rate is unset the backend returns 0.0, which must
  // render as an em dash -- never $0.000, which reads as "free".
  const RATE=S.rate==null?0.14:S.rate;
  $('#costRows').innerHTML=t.map(x=>{
    const prov=x.kind==='prov';
    const nm=prov?'openrouter':S.nodes[x.n].id;
    if(prov) return `<div class="row"><span>${nm}</span>
      <span class="mono">$${S.providers[0].cost.toFixed(3)}</span></div>
      <div class="unit" style="margin:-4px 0 6px">published by the provider</div>`;
    const n=S.nodes[x.n], watts=n.w*n.thr/100, tps=n.tps*n.thr/100;
    if(RATE<=0) return `<div class="row"><span>${nm}</span><span class="mono">\u2014</span></div>
      <div class="unit" style="margin:-4px 0 6px">set an electricity rate to price local generation</div>`;
    const usd=(watts/1000*RATE)/(tps*3600)*1e6;
    return `<div class="row"><span>${nm}</span><span class="mono">$${usd.toFixed(3)}</span></div>
      <div class="unit" style="margin:-4px 0 6px">from ${Math.round(watts)} W at ${tps.toFixed(0)} tok/s
        \u00b7 $${RATE.toFixed(2)}/kWh</div>`;
  }).join('');
}

