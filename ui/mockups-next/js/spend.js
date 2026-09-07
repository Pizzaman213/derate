/* derate mockup. Split so the file boundaries match the React port's
   package layout (Part G): what lives here lands in tabs/Spend.
   Classic scripts, loaded in order -- NOT ES modules, because every
   generated onclick= in this file resolves against global scope and
   module scope would break all twelve of them. */
// ---------- spend ----------
function spend(){
  const local=S.nodes.filter(n=>!n.remote).reduce((a,n)=>a+n.req,0);
  const managed=S.nodes.filter(n=>n.remote).reduce((a,n)=>a+n.req,0);
  const cloud=S.providers.reduce((a,p)=>a+p.req,0);
  const tot=local+managed+cloud;
  const pc=v=>tot>0?(v/tot*100):0;   // 0/0 rendered NaN% across the whole tab
  $('#lrSplit').innerHTML=
    `<span style="width:${pc(local)}%;background:var(--fill);color:var(--onfill)">local ${pc(local).toFixed(0)}%</span>
     <span style="width:${pc(managed)}%;background:var(--fill);opacity:.55;color:var(--onfill)">managed ${pc(managed).toFixed(0)}%</span>
     <span style="width:${pc(cloud)}%;background:var(--warn);color:#1A1917">cloud ${pc(cloud).toFixed(0)}%</span>`;
  $('#sLocal').textContent=pc(local).toFixed(0);
  $('#sReq').textContent=tot.toLocaleString();
  $('#sCost').textContent=money(S.spentToday);
  const allCloud=tot*0.0009*0.600;
  $('#sSaved').textContent=money(Math.max(0,allCloud-S.spentToday));
  const rows=[...S.nodes.map(n=>({nm:n.id,kind:n.remote?'managed remote':'local',req:n.req,cost:n.cost,
      spend:n.req*0.0009*n.cost})),
    ...S.providers.map(p=>({nm:p.id,kind:'cloud',req:p.req,cost:p.cost,spend:p.req*0.0009*p.cost}))];
  $('#spendTable').innerHTML=`<tr><th>Target</th><th>Kind</th><th style="text-align:right">Requests</th>
    <th style="text-align:right">$/Mtok</th><th style="text-align:right">Spent</th></tr>`+
    rows.map(r=>`<tr><td class="mono">${r.nm}</td><td class="unit">${r.kind}</td>
      <td class="num">${r.req.toLocaleString()}</td><td class="num">$${r.cost.toFixed(3)}</td>
      <td class="num">${money(r.spend)}</td></tr>`).join('');
}

