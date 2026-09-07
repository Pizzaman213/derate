/* derate mockup. Split so the file boundaries match the React port's
   package layout (Part G): what lives here lands in tabs/Settings.
   Classic scripts, loaded in order -- NOT ES modules, because every
   generated onclick= in this file resolves against global scope and
   module scope would break all twelve of them. */
// ---------- settings ----------
function settings(){
  // Was literal markup dressed as read state. Every row now traces to a field.
  const cc=document.getElementById('clusterCard');
  if(cc){
    const healthy=S.nodes.filter(n=>!n.remote||!S.managedOff).length;
    const totMem=S.nodes.reduce((a,n)=>a+n.tot,0);
    cc.innerHTML=[
      ['Cluster id','c-local'],
      ['Coordinator',S.nodes[0]?S.nodes[0].id:'\u2014'],
      ['Gateway','0.0.0.0:8080'],
      ['Discovery','mDNS \u00b7 _derate._tcp.local.'],
      ['Nodes',`${S.nodes.length} \u00b7 ${healthy} healthy`],
      ['Total memory',`${totMem.toFixed(0)} GiB`],
      ['Join token','\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022'],
    ].map(([k,v])=>`<div class="row"><span>${k}</span><span class="mono">${v}</span></div>`).join('');
  }
  // Every column here is a field serialize.node_payload already emits and the
  // old table simply did not show.
  $('#nodeTable').innerHTML=`<tr><th>Node</th><th>Address</th><th>Hardware</th>
      <th style="text-align:right">Memory</th><th style="text-align:right">Bandwidth</th>
      <th>Driver</th><th>Seen</th><th></th></tr>`+
    S.nodes.map((n,i)=>`<tr>
      <td class="mono">${n.id}${n.remote?' <span class="unit">remote</span>':''}</td>
      <td class="mono unit">${n.addr}</td>
      <td class="unit">${n.gpu} <span class="mut">· sm_${n.cc}</span></td>
      <td class="num">${n.adr.toFixed(1)} <span class="unit">of ${n.tot.toFixed(0)} GiB</span></td>
      <td class="num">${n.bw} <span class="unit">GB/s</span></td>
      <td class="unit mono">${n.drv}</td>
      <td class="unit">${n.seen}${n.miss?` <span style="color:var(--warn)">· ${n.miss} missed</span>`:''}</td>
      <td style="text-align:right"><button style="padding:3px 9px" onclick="rmNode(${i})">Remove</button></td></tr>`).join('')
    + `<tr><td colspan="8" class="unit" style="padding-top:9px">Addressable is what the fit gate
       budgets against, at 90% guardrail. On a unified-memory node the operating system shares that
       pool, so the nameplate total overstates what a model can actually have.</td></tr>`;
  $('#provTable').innerHTML=`<tr><th>Provider</th><th>Key reference</th><th>Models</th>
      <th style="text-align:right">$/Mtok</th><th style="text-align:right">Today</th>
      <th>State</th><th>Refreshed</th><th></th></tr>`+
    S.providers.map((p,i)=>{
      const state = p.block ? `<span style="color:var(--warn)">${p.block}</span>`
        : (p.healthy?'admitting':'<span style="color:var(--fault)">unhealthy</span>');
      const spend = p.budget!=null
        ? `$${p.spent.toFixed(2)} <span class="unit">of $${p.budget.toFixed(2)}</span>`
        : `$${p.spent.toFixed(2)}`;
      return `<tr><td class="mono">${p.id}</td>
        <td class="mono unit">${p.key}</td>
        <td class="num">${p.models||0}</td>
        <td class="num">$${p.cost.toFixed(3)}</td>
        <td class="num">${spend}</td>
        <td class="unit">${state}</td>
        <td class="unit">${p.refreshed||'\u2014'}</td>
        <td style="text-align:right"><button style="padding:3px 9px" onclick="rmProv(${i})">Remove</button></td></tr>`;
    }).join('')
    + `<tr><td colspan="8" class="unit" style="padding-top:9px">The key reference is the name of an
       environment variable, not a key. It is resolved at request time and never displayed, logged,
       or exported \u2014 there is no reveal control and adding one would be the bug.</td></tr>`;
}
function rmNode(i){
  if(S.nodes[i].slots.some(s=>s)){ alert(`${S.nodes[i].id} is running a deployment. Stop it first.`); return; }
  S.nodes.splice(i,1); roster(); settings(); render();
}
function rmProv(i){ S.providers.splice(i,1); roster(); sidebar(); settings(); render(); }
$('#addNode').onclick=()=>{
  const a=$('#naddr').value.trim(); if(!a) return;
  const rm=$('#nkind').value==='1';
  S.nodes.push({id:'node-'+(S.nodes.length+1),gpu:'probing…',unified:0,bw:0,remote:rm?1:0,
    lat:rm?60:0,rate:rm?1.0:0,slots:[null],w:0,t:0,cpu:0,tps:0,thr:100,cost:rm?0.4:0.03,req:0});
  $('#naddr').value=''; roster(); settings(); render();
};
$('#addProv').onclick=()=>{
  const k=$('#pkind').value, key=$('#pkey').value;
  if(!key){ alert('Enter an API key. It is stored as a reference and never displayed again.'); return; }
  S.providers.push({id:k,cost:0.5,key:k.toUpperCase()+'_API_KEY',on:1,req:0,models:0,spill:[]});
  $('#pkey').value=''; roster(); sidebar(); settings(); render();
};
S.rate=0.14; S.cap=5.00;
$('#rate').oninput=e=>{ const v=parseFloat(e.target.value); S.rate=isFinite(v)?v:0; sidebar(); spend(); };
$('#cap').oninput=e=>{ const v=parseFloat(e.target.value); S.cap=isFinite(v)?v:null; spend(); };
$('#cloudOff').onchange=e=>{ S.cloudOff=e.target.checked; roster(); sidebar(); render(); spend(); };
$('#managedOff').onchange=e=>{ S.managedOff=e.target.checked; roster(); sidebar(); render(); spend(); };

