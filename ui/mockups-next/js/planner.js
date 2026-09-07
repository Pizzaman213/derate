/* derate mockup. Split so the file boundaries match the React port's
   package layout (Part G): what lives here lands in dashboard/{PlannerBar,Verdict,LaunchCommand}.
   Classic scripts, loaded in order -- NOT ES modules, because every
   generated onclick= in this file resolves against global scope and
   module scope would break all twelve of them. */
// ---------- launch command ----------
// Mirrors _VLLM_COMMAND / _SGLANG_COMMAND in control_plane/deploy/flags.py.
// derate synthesizes a recipe per deployment whose command template
// references every knob, because a sparkrun flag the template does not mention
// is a SILENT no-op -- accepted, exit 0, and the setting never applied. Extra
// flags therefore have to be templated in too, not just passed along.
const RUNTIMES={
  vllm:{seqKey:'--max-num-seqs',ep:'--enable-expert-parallel',
    img:'ghcr.io/spark-arena/dgx-vllm-eugr-nightly:latest'},
  sglang:{seqKey:'--max-running-requests',ep:'--enable-ep-moe',
    img:'scitrera/dgx-spark-sglang:0.5.9-t5'}};
S.plan=null;
S.swap=null;
function renderCmd(){
  const box=document.getElementById('cmdOut'); if(!box) return;
  const P=S.plan;
  if(!P){ box.textContent='Press Plan to render the command.'; return; }
  const rt=RUNTIMES[P.runtime]||RUNTIMES.vllm;
  const bin=P.runtime==='sglang'?'python -m sglang.launch_server --model-path':'vllm serve';
  const L=[`${bin} ${P.model}`,
    `--served-model-name ${P.served}`,
    `--host 0.0.0.0`,`--port 8000`,
    `--tensor-parallel-size ${P.tp}`,
    `--pipeline-parallel-size ${P.pp}`,
    `--max-model-len ${P.ctx}`,
    `${rt.seqKey} ${P.seqs}`,
    `--gpu-memory-utilization 0.90`,
    `--trust-remote-code`];
  if(P.ep) L.push(rt.ep);
  const extra=(P.extra||'').trim();
  box.innerHTML=L.map((l,i)=>(i?'    ':'')+esc(l)).join(' \\\n')
    + (extra?' \\\n    <span class="ovr">'+esc(extra)+'</span>':'');
}
const esc=x=>String(x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
$('#cmdBtn').onclick=e=>{ const b=$('#cmdBox'), on=b.style.display==='none';
  b.style.display=on?'block':'none'; e.target.innerHTML='launch command '+(on?'&#9662;':'&#9656;'); };
['#extra','#runtime'].forEach(q=>$(q).addEventListener('input',()=>{ if(S.plan){
  S.plan.extra=$('#extra').value; S.plan.runtime=$('#runtime').value; renderCmd(); } }));

// Swapping keeps the served name, so every client keeps working across the change.
function beginSwap(i){
  const d=S.deps[i]; if(!d) return;
  S.swap={name:d.name,idx:i}; closeSheet();
  document.querySelector('.dest button[data-d=dash]').click();
  $('#mid').value=''; $('#ctx').value=d.ctx; $('#seqs').value=d.seqs;
  $('#swapbar').className='swapbar on';
  $('#swapbar').innerHTML=`<span>Replacing <b class="mono">${esc(d.name)}</b>. `
    +`It keeps its served name, so clients need no change. `
    +`It stops admitting only when the replacement is ready.</span>`
    +`<button style="margin-left:auto" onclick="cancelSwap()">Cancel</button>`;
  $('#mid').focus();
}
function cancelSwap(){ S.swap=null; $('#swapbar').className='swapbar'; $('#serveBtn').textContent='Serve'; }

// ---------- fit ----------
const USABLE=107.7,GIB=1024**3;
const BPP={fp32:4,bf16:2,fp16:2,fp8:1,mxfp4:0.53125,nvfp4:0.5625,q4_k_m:0.6125};
const shapes={'openai/gpt-oss-120b':{L:36,kv:8,hd:64,P:120e9,A:5.1e9,sw:128,full:18},
  'meta-llama/llama-3.3-70b-instruct':{L:80,kv:8,hd:128,P:70e9},
  'deepseek-ai/deepseek-v3':{L:61,kv:1,hd:576,P:671e9,A:37e9,mla:1},
  'qwen/qwen3-8b':{L:36,kv:8,hd:128,P:8e9},
  'mistralai/mistral-7b-instruct-v0.3':{L:32,kv:8,hd:128,P:7e9}};
function shapeOf(id){
  const k=id.trim().toLowerCase();
  if(!k) return null;                       // '' matched everything: s.includes('') is always true
  if(shapes[k]) return shapes[k];
  for(const s in shapes){ const base=s.split('/')[1];
    if(k===s||k===base||(k.length>=4&&(s.includes(k)||k.includes(base)))) return shapes[s]; }
  return null;}
$('#planBtn').onclick=()=>{
  const id=$('#mid').value,q=$('#quant').value,ctx=+$('#ctx').value||8192,seqs=+$('#seqs').value||1;
  const pl=$('#place').value,v=$('#verdict'),sh=shapeOf(id); $('#serveBtn').disabled=true;
  if(!sh){ v.className='verdict on bad';
    v.innerHTML=`<div class="vhead" style="color:var(--fault)">Could not resolve ${id}</div>
      <div class="mut">No config.json reachable. Check the id, or the repo may be gated.</div>`; return; }
  const bpp=BPP[q]||2,W=sh.P*bpp/GIB;
  const perTok=sh.mla?sh.L*sh.hd*2:2*sh.L*sh.kv*sh.hd*2;
  const effCtx=sh.sw?(sh.full*ctx+(sh.L-sh.full)*Math.min(sh.sw,ctx))/sh.L:ctx;
  const KV=perTok*effCtx*seqs/GIB,fixed=3.6;
  const cap=pl==='auto'?USABLE:(S.nodes[+pl].unified?USABLE:(S.nodes[+pl].remote?76:22));
  const solo=W+KV+fixed,two=W/2+KV/2+fixed,need=Math.ceil(W/(USABLE-fixed));
  const bw=pl==='auto'?273:S.nodes[+pl].bw;
  const tps=Math.round(bw*1e9/((sh.A||sh.P)*bpp)*0.55);
  const bar=(a,b,c)=>`<div class="budget"><span style="width:${Math.min(a/cap*100,100)}%;background:${c}"></span>
    <span style="width:${Math.max(0,Math.min(b/cap*100,100-a/cap*100))}%;background:${c};opacity:.5"></span></div>`;
  const free=S.nodes.reduce((a,n)=>a+n.slots.filter(s=>!s).length,0);
  const where=pl==='auto'?'the best free slot':S.nodes[+pl].id;
  const twoUp=(pl==='auto'&&solo>cap&&two<=USABLE);
  S.plan={model:id,served:S.swap?S.swap.name:(id.split('/').pop()||id),
    tp:1,pp:twoUp?2:1,ctx,seqs,ep:!!sh.A,runtime:$('#runtime').value,extra:$('#extra').value};
  renderCmd();

  if(solo<=cap){ v.className='verdict on';
    v.innerHTML=`<div class="vhead" style="color:var(--live)">Fits · 1 slot on ${where}</div>${bar(W,KV,'var(--fill)')}
      <div class="legend"><span><b>${W.toFixed(1)}</b> weights</span><span><b>${KV.toFixed(1)}</b> kv</span>
      <span><b>${(cap-solo).toFixed(1)} GiB</b> free of ${cap}</span></div>
      <div class="mut" style="margin-top:8px">Predicted ${tps} tok/s. ${free} slot${free===1?'':'s'} free across the cluster.</div>`;
    $('#serveBtn').disabled=free===0;
  } else if(pl==='auto'&&two<=USABLE){ v.className='verdict on';
    v.innerHTML=`<div class="vhead" style="color:var(--live)">Fits · PP 2 across both Sparks</div>${bar(W/2,KV/2,'var(--fill)')}
      <div class="legend"><span><b>${(W/2).toFixed(1)}</b> weights</span><span><b>${(KV/2).toFixed(1)}</b> kv</span>
      <span><b>${(USABLE-two).toFixed(1)} GiB</b> free per node</span></div>
      <div class="mut" style="margin-top:8px">Pipeline parallel over tensor parallel: measured all-reduce is ${S.link} GB/s,
      below the 40 GB/s threshold. brev-h100 excluded, no measured fabric to it.</div>`;
    $('#serveBtn').disabled=false;
  } else if(W+fixed>cap&&(pl!=='auto'||W/2+fixed>USABLE)){ v.className='verdict on bad';
    v.innerHTML=`<div class="vhead" style="color:var(--fault)">Won't fit · limiting term: weights</div>${bar(W,0,'var(--fault)')}
      <div class="legend"><span><b>${W.toFixed(1)} GiB</b> weights</span><span><b>${cap}</b> GiB usable</span></div>
      <div style="margin-top:8px">Weights alone exceed the budget at zero context on ${where}.
      ${pl==='auto'?`Needs at least ${need} nodes at ${q}, or a smaller quantization.`:'Try auto placement or a smaller quantization.'}</div>`;
  } else {
    const per=pl==='auto'?2:1, budget=cap-(W/per+fixed);
    let mx=Math.floor(budget*GIB/(perTok*seqs/per)); mx=Math.max(512,Math.floor(mx/512)*512);
    v.className='verdict on bad';
    v.innerHTML=`<div class="vhead" style="color:var(--fault)">Won't fit · limiting term: kv_cache</div>${bar(W/per,KV/per,'var(--fault)')}
      <div class="legend"><span><b>${(W/per).toFixed(1)}</b> weights</span><span><b>${(KV/per).toFixed(1)}</b> kv</span>
      <span><b>${cap}</b> GiB usable</span></div>
      <div style="margin-top:8px">KV cache is the problem: ${(KV/per).toFixed(1)} GiB at ${ctx.toLocaleString()} tokens
      across ${seqs} sequences. Drop context to ${mx.toLocaleString()}, cut concurrency, or quantize the KV cache to fp8.</div>
      <button style="margin-top:10px" onclick="document.getElementById('ctx').value=${mx};document.getElementById('planBtn').click()">
      Retry at ${mx.toLocaleString()} tokens</button>`;
  }
};
$('#serveBtn').onclick=e=>{ e.target.disabled=1;
  e.target.textContent=S.swap?'Swapping…':'Launching…';
  setTimeout(()=>{ e.target.textContent='Serve';
    $('#verdict').insertAdjacentHTML('beforeend',
      `<div class="mut" style="margin-top:10px;padding-top:9px;border-top:1px solid var(--rule)">
       Launched. Now in <span class="mono">/v1/models</span>.</div>`); },1500); };

