// Text stress for the gallery. `stressed(s)` returns the sample string the
// gallery should show under the active mode, read from the URL by main.tsx
// (`?stress=long|url|rtl|emoji|empty`). Under the static style guide there is
// no window, so the original string comes back.

export type Stress = 'none' | 'long' | 'url' | 'rtl' | 'emoji' | 'empty'

export function activeStress(): Stress {
  const g = globalThis as { __derateGalleryStress?: Stress }
  return g.__derateGalleryStress ?? 'none'
}

const LONG =
  ' The fit gate measured 88.1 GiB usable on spark-01 against 92.2 GiB required at 32768 context with 8 concurrent sequences, and the planner has no second node whose measured link clears the 2.5 GB/s floor tensor parallelism needs, so this is the whole answer and not a summary of one.'
const URL_ = 'https://huggingface.co/RedHatAI/Meta-Llama-3.1-405B-Instruct-quantized.w4a16/resolve/main/model-00001-of-00086.safetensors?download=true&revision=8f3c1a9b7e2d4c6f0a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9'
const RTL = 'تجاوزت أوزان النموذج الذاكرة القابلة للاستخدام على spark-01 بمقدار 4.1 GiB؛ خفّض طول السياق إلى 24576.'
const EMOJI = '🚀 Qwen3-30B-A3B 🧠 fits on spark-01 ✅ 88.1 GiB 📦 2.94 GB/s 🔗 — 👍👍👍'

export function stressed(s: string): string {
  switch (activeStress()) {
    case 'long': return s + LONG + LONG
    case 'url': return `${s} ${URL_}`
    case 'rtl': return RTL
    case 'emoji': return `${EMOJI} ${s}`
    case 'empty': return ''
    default: return s
  }
}
