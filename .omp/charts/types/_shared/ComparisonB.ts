// Shared by lollipop, dumbbell and waterfall.

/** Unique, stable keys for labels that may repeat: "a", "a#2", "a#3"... */
export function uniqueKeys(labels: string[]): string[] {
  const seen = new Map<string, number>()
  return labels.map((l) => {
    const n = (seen.get(l) ?? 0) + 1
    seen.set(l, n)
    return n === 1 ? l : `${l}#${n}`
  })
}

/** "+1.2k" / "−300" / "0": a signed change. */
export function signed(v: number, format: (v: number) => string): string {
  return v > 0 ? `+${format(v)}` : format(v)
}
