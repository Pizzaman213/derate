import type { ModelDetail } from '../../api/types'

/** The architectural facts, as tags.
 *
 *  `.pill` rather than `.chips button`: these report, they do not toggle, and
 *  `.chips button` is styled for something you can press. Category is carried
 *  by the text, never by colour -- colour in this app means state.
 *
 *  Every number here arrives computed. Nothing is divided or summed in the
 *  browser, so a chip cannot disagree with the fit gate about the same model.
 */
export function CapabilityChips({ detail }: { detail: ModelDetail }) {
  const c = detail.capabilities
  const chips: { key: string; text: string; title: string }[] = []

  if (c.moe.present) {
    const active =
      c.moe.active_fraction != null ? ` · ${(c.moe.active_fraction * 100).toFixed(1)}% active` : ''
    chips.push({
      key: 'moe',
      text: `MoE ${c.moe.num_experts}/${c.moe.num_experts_per_token}${active}`,
      title: 'Mixture of experts: total experts / experts routed per token',
    })
  }
  if (c.mla.present) {
    chips.push({
      key: 'mla',
      text: `MLA ${c.mla.cached_width_per_layer}`,
      title:
        'Multi-head latent attention. The figure is the width cached per layer ' +
        'per token — the latent plus the decoupled RoPE dimension, not the latent alone.',
    })
  }
  if (c.gqa.present) {
    chips.push({
      key: 'gqa',
      text: `GQA ${c.gqa.num_attention_heads}:${c.gqa.num_kv_heads}`,
      title: 'Grouped-query attention: attention heads per key/value head',
    })
  }
  if (c.sliding_window.present) {
    const full = c.sliding_window.layers_with_full_attention
    const detailText =
      full == null
        ? `window ${c.sliding_window.window}`
        : `window ${c.sliding_window.window} · ${full} of ${c.sliding_window.num_layers} full`
    chips.push({
      key: 'swa',
      text: detailText,
      title:
        'Sliding-window attention. Layers listed as "full" cache the whole ' +
        'context; the rest cache only the window.',
    })
  }
  if (c.vision.present) {
    chips.push({
      key: 'vision',
      text: `vision ${fmtB(c.vision.vision_params)}`,
      title: 'Vision tower. Replicated on every node when sharding, never split.',
    })
  }
  if (c.mtp.present) {
    // Stated, not hidden. The checkpoint carries this module and the hub's
    // weight index counts it, but no runtime loads it unless speculative
    // decoding is on -- so total_params excludes it and the download is bigger
    // than the load. A reader comparing our figure against the repository's
    // own parameter count needs to see why they differ.
    chips.push({
      key: 'mtp',
      text: `MTP ${fmtB(c.mtp.params)} not loaded`,
      title:
        c.mtp.note ??
        'Multi-token prediction module: present in the checkpoint, not loaded ' +
          'unless speculative decoding is enabled.',
    })
  }
  if (c.context.max_position_embeddings) {
    chips.push({
      key: 'ctx',
      text: `${c.context.max_position_embeddings.toLocaleString()} ctx`,
      title: 'Maximum position embeddings the config declares',
    })
  }

  if (!chips.length) return null

  return (
    <div className="chips" style={{ marginBottom: 4 }}>
      {chips.map((chip) => (
        <span key={chip.key} className="pill" title={chip.title}>
          {chip.text}
        </span>
      ))}
    </div>
  )
}

/** Parameter counts, in the unit people say them in. */
function fmtB(n: number | null): string {
  if (n == null) return '—'
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`
  if (n >= 1e6) return `${(n / 1e6).toFixed(0)}M`
  return String(n)
}
