import type { Provider, ProviderKindSpec } from '../../api/types'

// Which configured providers can be told to fetch weights, and what is
// already on one. Pure, so the verifier can drive it and so the two screens
// that ask -- the quantization ladder and the pull card -- cannot answer
// differently.

/** The providers a pull can target.
 *
 *  Derived from the server's kind table rather than a literal `'ollama'`.
 *  `supports_pull` is `bool(pull_path)` on the kind spec, so a build that
 *  teaches a second kind to host its own weights becomes a target here with
 *  no change on this side -- which is the whole reason `GET
 *  /api/providers/kinds` exists instead of the form restating the table.
 */
export function pullableProviders(
  providers: Provider[] | null | undefined,
  kinds: ProviderKindSpec[] | null | undefined,
): Provider[] {
  const pullable = new Set(
    (kinds ?? []).filter((k) => k.supports_pull).map((k) => k.kind),
  )
  return (providers ?? []).filter((p) => pullable.has(p.kind))
}

/** Whether a provider already holds this exact model.
 *
 *  Case-insensitive, and deliberately only ever decorative. Ollama echoes an
 *  `hf.co/<repo>:<tag>` ref back through its catalogue, but whether it
 *  preserves the case of the tag is not something this codebase has verified
 *  against a live server -- so a mismatch must cost a chip that failed to
 *  appear, never a button that refuses to work.
 */
export function alreadyOn(provider: Provider, ref: string | null): boolean {
  if (!ref) return false
  const want = ref.trim().toLowerCase()
  return provider.models.some(
    (m) =>
      m.upstream_id.trim().toLowerCase() === want ||
      m.served_name.trim().toLowerCase() === want,
  )
}
