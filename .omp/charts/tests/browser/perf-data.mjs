// Synthetic inputs for the perf budgets: 1,000 marks on the SVG path (< 100 ms) and
// 50,000 on the canvas path (< 500 ms), for the chart types whose marks scale with rows.
// `gen` is a function body taking `n`, run inside the page (deterministic LCG).

const LCG = 'let s=12345;const r=()=>(s=(s*1664525+1013904223)%4294967296)/4294967296;'

export const CASES = {
  bar: `${LCG}return Array.from({length:n},(_,i)=>({label:'k'+i,value:Math.round(r()*1000)}))`,
  scatter: `${LCG}return Array.from({length:n},()=>({x:r()*100,y:r()*100}))`,
  line: `${LCG}let v=100;return Array.from({length:n},(_,i)=>({x:new Date(Date.UTC(2020,0,1)+i*36e5).toISOString(),y:(v+=r()-0.5)}))`,
  beeswarm: `${LCG}return Array.from({length:n},()=>({value:r()*100}))`,
}

/** [{ type, n, gen }] for every case at 1k and 50k. */
export function perfCases() {
  return Object.entries(CASES).flatMap(([type, gen]) => [1000, 50000].map((n) => ({ type, n, gen })))
}
