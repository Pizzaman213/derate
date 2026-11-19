import { describe, expect, it } from 'vitest'
// @ts-expect-error -- plain ESM build script, no types
import { build, gates } from '../../theme/build.mjs'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

describe('theme tokens', () => {
  const out = build() as { css: string; ts: string; tokens: Record<string, Record<string, string>> }
  it('generated files are fresh', () => {
    expect(readFileSync(join(import.meta.dirname, '../../theme/chart-tokens.css'), 'utf8')).toBe(out.css)
    expect(readFileSync(join(import.meta.dirname, '../../theme/tokens.generated.ts'), 'utf8')).toBe(out.ts)
  })
  it('categorical palette passes contrast and colour-vision gates in every theme', () => {
    expect((gates(out.tokens) as { findings: string[] }).findings).toEqual([])
  })
  it('defines light, dark and high-contrast, scoped and media-driven', () => {
    expect(out.css).toMatch(/\.oc-root \{/)
    expect(out.css).toMatch(/\.oc-root\[data-theme='dark'\]/)
    expect(out.css).toMatch(/\.oc-root\[data-theme='high-contrast'\]/)
    expect(out.css).toMatch(/@media \(prefers-color-scheme: dark\)/)
    expect(out.css).toMatch(/@media \(prefers-contrast: more\)/)
  })
})
