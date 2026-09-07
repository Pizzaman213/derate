/* derate mockup. Split so the file boundaries match the React port's
   package layout (Part G): what lives here lands in sidebar/SelectionCard.
   Classic scripts, loaded in order -- NOT ES modules, because every
   generated onclick= in this file resolves against global scope and
   module scope would break all twelve of them. */
// ---------- inspector ----------
function inspect(i){
  const n=S.nodes[i];
  $('#cardBody').innerHTML=`
    <div style="display:flex;justify-content:space-between;align-items:baseline">
      <span class="label mono" style="font-size:16px">${n.id}</span>
      <button onclick="document.getElementById('sheet').classList.remove('on')">Close</button></div>
    <div class="unit" style="margin:4px 0 14px">${n.gpu} · ${n.unified?'unified memory':'discrete'} · ${n.bw} GB/s${n.remote?' · managed remote':''}</div>
    <div class="row"><span>decode</span><span class="mono">${(n.tps*n.thr/100).toFixed(1)} tok/s</span></div>
    <div class="row"><span>power</span><span class="mono">${Math.round(n.w*n.thr/100)} W</span></div>
    <div class="row"><span>temperature</span><span class="mono">${n.t} °C</span></div>
    <div class="row"><span>GPU utilisation</span><span class="mono">${n.cpu} %</span></div>
    <div class="row"><span>slot</span><span class="mono">${n.slots[0]?n.slots[0].m:'free'}</span></div>
    <div class="row"><span>requests today</span><span class="mono">${n.req.toLocaleString()}</span></div>
    ${n.remote?`<div class="row"><span>latency</span><span class="mono">${n.lat} ms</span></div>
      <div class="row"><span>rate</span><span class="mono">$${(n.rate||0).toFixed(2)}/hr</span></div>`:''}
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--rule)">
      <label class="unit" for="thr">Simulate throttling · ${n.thr}%</label>
      <input class="throttle" type="range" id="thr" min="20" max="100" step="5" value="${n.thr}" oninput="setThr(${i},this.value)">
      <div class="unit">${n.remote
        ? 'Managed by us and fully instrumented, but no measured fabric to the local nodes. It can hold a deployment; it can never be part of one that spans nodes.'
        : n.unified ? 'CPU and GPU share one 273 GB/s bus, so CPU load reduces decode bandwidth directly.'
        : 'Each card has its own memory bandwidth, so throttling one slot does not affect the others.'}</div>
    </div>`;
  $('#sheet').classList.add('on');
}
function setThr(i,v){ S.nodes[i].thr=+v; roster(); inspect(i); }
function closeSheet(){ S.openDep=null; $('#sheet').classList.remove('on'); $('#cardBody').className='card'; }
$('#sheet').onclick=e=>{ if(e.target.id==='sheet') closeSheet(); };

// ---------- model inspector ----------
// Every figure maps to a field the control plane actually produces: ttft_ms and
// mean_duration_s are EWMAs on TargetStats, "queued" is TargetStats.outstanding,
// and ms/token is arithmetic on the decode rate. There are no percentiles
// anywhere in the system, so none are shown.
const fmtN=(v,d=0)=>v==null||!isFinite(v)?'\u2014':v.toFixed(d);
function inspectDep(i){
  const d=S.deps[i]; if(!d) return;
  S.openDep=i; S.sel=i; sidebar(); depsStrip(); render();
  const t=tgts(d), w=curW(d), h=S.hist[d.name]||[], tps=h.length?h[h.length-1]:d.tps;
  const nodes=d.span?d.span:(d.solo?[d.solo.n]:[]);
  const mx=Math.max(...h,1)*1.2;
  const pts=h.map((v,j)=>`${j/Math.max(1,h.length-1)*100},${26-v/mx*24}`).join(' ');

  const rows=t.map((x,k)=>{
    const prov=x.kind==='prov';
    const nm=prov?'openrouter':S.nodes[x.n].id;
    const cost=prov?S.providers[0].cost:S.nodes[x.n].cost;
    const req=prov?S.providers[0].req:S.nodes[x.n].req;
    const str=prov?0.55:Math.min(1,S.nodes[x.n].tps*S.nodes[x.n].thr/100/200);
    return `<tr><td class="mono">${nm}</td><td class="unit">${prov?'remote':x.kind}</td>
      <td class="num">${(w[k]*100).toFixed(0)}%</td><td class="num">${Math.round(w[k]*(d.queue||0))}</td>
      <td class="num">${str.toFixed(2)} <span class="unit">${prov?'default':S.nodes[x.n].src}</span></td>
      <td class="num">$${cost.toFixed(3)}</td><td class="num">${req.toLocaleString()}</td></tr>`;}).join('');

  const mem=nodes.map(n=>{
    const nd=S.nodes[n], sl=(d.solo&&d.solo.g!=null)?nd.slots[d.solo.g]:nd.slots[0];
    return `<div class="slot"><span class="mono unit" style="width:74px">${nd.id}</span>
      <div class="bar"><i style="width:${sl?sl.pc:0}%"></i></div>
      <span class="mono unit" style="width:34px;text-align:right">${sl?sl.pc:0}%</span></div>`;}).join('');

  const why=d.span
    ? `<div>Pipeline parallel across ${d.span.length} nodes, because measured all-reduce is ${S.link} GB/s and target concurrency is ${d.seqs}.</div>
       <div class="mut" style="margin-top:6px">Rejected</div>
       <div>TP ${d.span.length} \u2014 ${S.link} GB/s is below the 40 GB/s threshold.</div>
       <div>Spanning brev-h100 \u2014 remote node, no measured fabric. Routing only.</div>`
    : `<div>Fits in one slot on ${S.nodes[d.solo.n].id}, so it is placed whole rather than sharded.</div>
       <div class="mut" style="margin-top:6px">Rejected</div>
       <div>Sharding \u2014 would pay a handoff per token to solve a capacity problem that does not exist.</div>`;

  $('#cardBody').className='card wide';
  $('#cardBody').innerHTML=`
    <div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px">
      <span class="label mono" style="font-size:17px">${d.name}</span>
      <span class="unit" style="margin-right:auto"><span class="dot" style="background:var(--live)"></span> ready</span>
      <button onclick="beginSwap(${i})">Change model</button>
      <button onclick="closeSheet()">Close</button></div>
    <div class="unit" style="margin:5px 0 0">${d.plan} \u00b7 ${d.rt} \u00b7 ${d.dtype} \u00b7 ${d.ctx.toLocaleString()} ctx \u00b7 ${d.seqs} seqs \u00b7 up ${d.up}</div>

    <div class="sub" style="border:none;padding-top:0">latency and throughput</div>
    <div class="quad">
      <div><div class="readout" id="md-tps">${fmtN(tps)}</div><div class="unit">tok/s aggregate, all streams</div></div>
      <div><div class="readout" id="md-dtps">${fmtN(d.dtps)}</div><div class="unit">tok/s per stream</div></div>
      <div><div class="readout" id="md-itl">${fmtN(d.dtps>0?1000/d.dtps:null,1)}</div><div class="unit">ms between tokens</div></div>
      <div><div class="readout" id="md-ttft">${fmtN(d.ttft)}</div><div class="unit">ms to first token</div></div>
      <div><div class="readout" id="md-q">${d.queue}</div><div class="unit">queued now</div></div>
    </div>
    <svg viewBox="0 0 100 28" preserveAspectRatio="none" style="width:100%;height:38px;margin-top:10px">
      <polyline points="${pts}" fill="none" stroke="var(--ink)" stroke-width="1" vector-effect="non-scaling-stroke"/></svg>
    <div class="unit">Aggregate is a trailing-window sum across every stream; per stream is one request's own
      decode rate, averaged over requests \u2014 they answer different questions and only match at concurrency 1.
      Mean request ${fmtN(d.dur,1)} s. All are exponential moving averages; the control plane keeps no percentiles.
      For a non-streaming response there is no first-token boundary, so per-stream decode falls back to whole-request
      duration and silently includes prefill.</div>

    <div class="sub">where a request's time goes</div>
    ${(()=>{
      // Prefill time is REAL and already measured: proxy.py sets
      // ttft_s = now - started on the first chunk, decode_s = now - first_token_at.
      // This split needs no new instrumentation. What does NOT exist is prefill
      // THROUGHPUT -- that needs prompt tokens, and the gateway has no tokenizer
      // (estimate_prompt_tokens is len(chars)/4). Charting a chars/4 estimate as
      // a token rate would invent a benchmark, so this shows time only.
      if(!d.stream) return `<div class="unit">This model does not stream, so there is no
        first-token boundary and the phases cannot be separated \u2014 the accounting records
        the whole request as its own decode window. Mean request ${fmtN(d.dur,1)} s.</div>`;
      if(d.ttft==null||d.dur==null) return `<div class="unit">Not yet observed.</div>`;
      const pre=d.ttft/1000, dec=Math.max(0,d.dur-pre), tot=pre+dec, pp=pre/tot*100;
      return `<div class="phase">
          <span style="width:${pp.toFixed(1)}%;background:var(--fill);color:var(--onfill)">${pp>=12?'prefill':''}</span>
          <span style="width:${(100-pp).toFixed(1)}%;background:var(--fill);opacity:.45;color:var(--onfill)">decode</span>
        </div>
        <div class="legend"><span><b>${fmtN(pre*1000)} ms</b> prefill (${pp.toFixed(1)}%)</span>
          <span><b>${fmtN(dec,1)} s</b> decode</span>
          <span><b>${fmtN(tot,1)} s</b> mean request</span></div>
        <div class="unit" style="margin-top:6px">Prefill processes the whole prompt at once and is
          compute-bound; decode emits one token at a time and is memory-bandwidth-bound. The planner
          and the fit gate reason about decode bandwidth, not this.</div>`;
    })()}
    <div class="sub">targets \u00b7 ${$('#policy').value}</div>
    <table><tr><th>Target</th><th>Kind</th><th style="text-align:right">Share</th>
      <th style="text-align:right">In flight</th><th style="text-align:right">Strength</th>
      <th style="text-align:right">$/Mtok</th><th style="text-align:right">Requests</th></tr>${rows}</table>

    <div class="sub">placement</div>
    <div class="row"><span>Plan</span><span class="mono">${d.plan}</span></div>
    <div class="row"><span>Nodes</span><span class="mono">${nodes.map(n=>S.nodes[n].id).join(', ')||'\u2014'}</span></div>
    <div class="row"><span>Measured all-reduce</span><span class="mono">${d.span?S.link+' GB/s':'\u2014'}</span></div>
    <div class="why on" style="margin-top:8px">${why}</div>

    <div class="sub">memory on each node</div>${mem||'<div class="unit">\u2014</div>'}`;
  $('#sheet').classList.add('on');
}

