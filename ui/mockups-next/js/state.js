/* derate mockup. Split so the file boundaries match the React port's
   package layout (Part G): what lives here lands in state/ + api/.
   Classic scripts, loaded in order -- NOT ES modules, because every
   generated onclick= in this file resolves against global scope and
   module scope would break all twelve of them. */
const $=s=>document.querySelector(s), NS='http://www.w3.org/2000/svg';
const el=(t,a,x)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);
  if(x!=null)e.textContent=x;return e;};
const money=v=>'$'+v.toFixed(2);

const S={
  joined:1, saturated:0, link:10.2, sel:0, cloudOff:0, managedOff:0, spentToday:0.41, reqToday:1284,
  nodes:[
    {id:'spark-01',gpu:'NVIDIA GB10',addr:'192.168.11.13',cc:'12.1',drv:'580.95.05',tot:128.0,adr:119.7,seen:'3 s ago',miss:0,unified:1,bw:273,remote:0,slots:[null],w:71,t:62,cpu:34,tps:64,thr:100,cost:0.019,req:0},
    {id:'spark-02',gpu:'NVIDIA GB10',addr:'192.168.11.14',cc:'12.1',drv:'580.95.05',tot:128.0,adr:119.7,seen:'2 s ago',miss:0,unified:1,bw:273,remote:0,slots:[null],w:68,t:60,cpu:31,tps:63,thr:100,cost:0.019,req:0},
    {id:'ws-3090',gpu:'RTX 3090 x4',addr:'192.168.11.20',cc:'8.6',drv:'570.86.15',tot:96.0,adr:88.0,seen:'4 s ago',miss:1,unified:0,bw:936,remote:0,
      slots:[{m:'mistral-7b',pc:70},{m:'embed-v3',pc:45},null,null],w:212,t:68,cpu:12,tps:64,thr:100,cost:0.048,req:0},
    {id:'brev-h100',gpu:'H100 80GB',addr:'10.0.4.19',cc:'9.0',drv:'560.35.03',tot:80.0,adr:76.0,seen:'11 s ago',miss:0,unified:0,bw:3350,remote:1,lat:41,rate:2.30,
      slots:[null],w:340,t:54,cpu:8,tps:186,thr:100,cost:0.310,req:0}
  ],
  // Shaped like control_plane/links/record.py: the measurement, plus an
  // annotation carrying HOW it was obtained. record.py:1-13 is explicit that a
  // number from ib_write_bw and scaled is a different KIND of fact from one
  // NCCL produced, and that the difference has to survive the trip to the UI.
  // Today serialize.link_payload drops the whole annotation -- that is M-7.
  links:[
    {a:0,b:1,measured:1,method:'nccl-tests',ar:10.2,sr:11.4,lat:8.4,gdr:1,
     age:'2 h ago',estimated:0,raw:null,scale:null,ports:[2,2],portsOn:'spark-01',
     gdrBy:'nvidia-smi topo: GPUDirect RDMA over ConnectX-7',dur:47,
     notes:['all-reduce swept 8 B to 1 GiB; peak taken at 512 MiB',
            'both QSFP cages up at 200 Gb/sec, InfiniBand']},
    {a:0,b:2,measured:1,method:'ib_write_bw',ar:4.3,sr:4.6,lat:31.0,gdr:0,
     age:'6 h ago',estimated:1,raw:10.24,scale:0.42,ports:[1,2],portsOn:'ws-3090',
     gdrBy:'nvidia-smi topo: no GPUDirect RDMA path',dur:12,
     notes:['derived from ib_write_bw: 10.24 GB/s raw RDMA scaled by 0.42 to approximate the GDR-disabled NCCL path',
            'raw RDMA is not NCCL bandwidth and is not reported as such',
            '1 of 2 QSFP cages up; the second is down, so this is roughly half the fabric']},
    {a:1,b:2,measured:0}
  ],
  providers:[{id:'openrouter',cost:0.600,key:'OPENROUTER_API_KEY',on:1,req:0,healthy:1,block:null,retry:0,budget:5.00,spent:0.058,tokIn:41200,tokOut:9840,unpriced:0,refreshed:'18 m ago',prio:10,
    models:318,spill:['gpt-oss-120b']}]
};
// Fixed, not random. A demo value is fine; a value that changes every reload
// is not, and a value that decides a provenance LABEL is the exact failure
// this project exists to prevent. strength_source is a backend field
// (measured|predicted|bandwidth|default), not a threshold on a request count.
[[0,412],[1,388],[2,1386],[3,240]].forEach(([i,v])=>{ if(S.nodes[i]) S.nodes[i].req=v; });
S.nodes.forEach(n=>{ n.src = n.remote ? 'predicted' : 'measured'; });
S.providers[0].req=96;

function setJoined(){ S.joined=1;
  S.nodes[0].slots=[{m:'gpt-oss-120b',pc:78}]; S.nodes[1].slots=[{m:'gpt-oss-120b',pc:74}];
  S.nodes[3].slots=[{m:'gpt-oss-120b',pc:62}];
  S.deps=[{name:'gpt-oss-120b',plan:'PP 2',span:[0,1],remote:[3],prov:1,tps:127},
    {name:'mistral-7b',plan:'1 slot',solo:{n:2,g:0},tps:64},
    {name:'embed-v3',plan:'1 slot',solo:{n:2,g:1},tps:41}]; decorate(); }
function setSplit(){ S.joined=0;
  S.nodes[0].slots=[{m:'qwen3-8b',pc:34}]; S.nodes[1].slots=[{m:'llama-3.1-8b',pc:41}];
  S.nodes[3].slots=[{m:'qwen3-coder-30b',pc:58}];
  S.deps=[{name:'qwen3-8b',plan:'1 slot',solo:{n:0},tps:212},
    {name:'llama-3.1-8b',plan:'1 slot',solo:{n:1},tps:198},
    {name:'qwen3-coder-30b',plan:'1 slot · remote',solo:{n:3},tps:186},
    {name:'mistral-7b',plan:'1 slot',solo:{n:2,g:0},tps:64},
    {name:'embed-v3',plan:'1 slot',solo:{n:2,g:1},tps:41}]; decorate(); }
const DETAIL={
  'gpt-oss-120b'   :{ttft:184,dur:6.2,queue:3,ctx:8192, seqs:16,dtype:'mxfp4',rt:'vllm',up:'2h 14m',req:620,dtps:42,stream:1},
  'mistral-7b'     :{ttft:41 ,dur:1.9,queue:0,ctx:16384,seqs:8, dtype:'fp8',  rt:'vllm',up:'6h 02m',req:284,dtps:64,stream:1},
  'embed-v3'       :{ttft:12 ,dur:0.3,queue:1,ctx:512,  seqs:32,dtype:'bf16', rt:'vllm',up:'6h 02m',req:1102,dtps:41,stream:0},
  'qwen3-8b'       :{ttft:38 ,dur:2.1,queue:1,ctx:32768,seqs:16,dtype:'bf16', rt:'vllm',up:'11m',req:210,dtps:96,stream:1},
  'llama-3.1-8b'   :{ttft:44 ,dur:2.4,queue:0,ctx:32768,seqs:16,dtype:'bf16', rt:'vllm',up:'11m',req:176,dtps:88,stream:1},
  'qwen3-coder-30b':{ttft:96 ,dur:4.8,queue:2,ctx:65536,seqs:8, dtype:'fp8',  rt:'vllm',up:'11m',req:95,dtps:58,stream:1}};
function decorate(){ S.deps.forEach(d=>Object.assign(d,
  DETAIL[d.name]||{ttft:null,dur:null,dtps:null,stream:1,queue:0,ctx:8192,seqs:8,dtype:'bf16',rt:'vllm',up:'—'})); }
setJoined();
S.hist={};

const dep=()=>S.deps[Math.min(S.sel,S.deps.length-1)];
function tgts(d){
  const a=[];
  if(d.span) d.span.forEach(n=>a.push({n,kind:'local'}));
  else if(d.solo) a.push({...d.solo,kind:S.nodes[d.solo.n].remote?'remote':'local'});
  (d.remote||[]).forEach(n=>{ if(!S.managedOff) a.push({n,kind:'remote'}); });
  const _p=S.providers[0];
  if(!S.cloudOff && _p && _p.on && _p.spill.includes(d.name)) a.push({p:0,kind:'prov'});
  return a;
}

