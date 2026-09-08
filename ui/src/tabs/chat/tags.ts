import type { CSSProperties } from 'react'

/** Which model a turn went to, drawn as a colour.
 *
 *  This tab lets the model change between turns -- the picker writes `?dep=`
 *  and the next send goes wherever it points -- so a transcript is not
 *  necessarily a conversation with one model. Until this module the only thing
 *  on screen that said so was the name over each ANSWER. A question carried no
 *  destination at all, and two answers from two different models were the same
 *  grey as each other, which made "compare these two models on the same
 *  prompt" a thing you did by counting turns.
 *
 *  Four rules, and each is the reason a shorter version was rejected.
 *
 *  1. SLOTS ARE ASSIGNED BY ORDER OF FIRST APPEARANCE, not by hashing the
 *     name. A hash is stable across transcripts, which sounds like the better
 *     property and is not: it can hand two models in the SAME transcript the
 *     same slot, and the only place the colour is ever read is the place where
 *     that must not happen. First appearance is unique by construction, and it
 *     is stable within a transcript for the same reason -- turns are appended
 *     and never reordered, so a turn's colour is fixed the moment it is first
 *     drawn and no later send can change it.
 *
 *  2. A NINTH MODEL GETS NO COLOUR, rather than the first model's again.
 *     Wrapping would draw two different models identically, which is worse
 *     than drawing one of them plainly: an absent mark says "read the name",
 *     a wrong mark says "these are the same model". The name is always there
 *     to fall back on, which is what makes the plain block a complete answer
 *     rather than a broken one.
 *
 *  3. THE COLOUR IS NEVER THE ONLY MARK. Every block still spells its model
 *     out in the label, and the second four slots are the same four hues with
 *     a DASHED rule instead of a solid one -- so eight identities survive a
 *     greyscale screenshot and a reader who does not separate these hues, on
 *     the same argument `--stream` and `--batch` are already split by hue and
 *     block width in tokens.css.
 *
 *  4. THE HUES ARE NOT THE STATUS HUES. `--tag-*` is deliberately clear of
 *     `--live`, `--warn` and `--fault` -- nothing is within 40 degrees of one,
 *     asserted in tags.check.mjs -- because a model's identity must never be
 *     readable as a verdict on it. It is clear of `--flow`/`--stream`/`--batch`
 *     as tokens too, even where a value lands nearby, because those mean
 *     something about a request and this means nothing but "the same model as
 *     that other block".
 *
 *  Two things this has to answer to.
 *
 *  `agents/H-ui.md`: "Nothing is coloured decoratively. If it has colour, it is
 *  reporting state." This is the second exception in the app and it is taken on
 *  the same terms as the first: the colour here IS the state. Which model a
 *  turn went to was not on screen at all for a question, and is the one fact
 *  somebody comparing two models on one prompt is reading for. The name is
 *  printed beside it for exactly that reason -- a mark that were only decorative
 *  would not need one.
 *
 *  `tabs/models/owner.ts` already hashes a publisher to one of ten hues, and
 *  this is NOT that function reused. Three reasons, in order of weight. A hash
 *  can collide, and on a grid of ninety cards two publishers sharing a hue is
 *  noise; here it would be the specific lie the colour exists to prevent, since
 *  the only question ever asked of it is "did these two blocks go to the same
 *  model". Its palette spans the amber and green families -- `hsl(44 …)`,
 *  `hsl(140 …)` -- which is survivable beside a card and not beside a turn that
 *  can carry a refusal rendered in `--fault`. And it is raw `hsl()` literals
 *  with no dark variant, which is fine for the one screen it is scoped to and
 *  not for a surface read in both themes.
 */

/** Distinct hues in the ramp. `tokens.css` defines `--tag-1` .. `--tag-4`. */
export const TAG_HUES = 4

/** Distinct identities: each hue solid, then each hue dashed. Past this a
 *  model is uncoloured -- see rule 2. */
export const TAG_SLOTS = TAG_HUES * 2

/** Model name -> slot, in order of first appearance. Names are the turns'
 *  destinations in transcript order; `null` (a turn with no model on it) is
 *  skipped rather than given a slot of its own, and a name past `TAG_SLOTS`
 *  is left out of the map entirely. */
export function tagsFor(names: readonly (string | null)[]): Map<string, number> {
  const slots = new Map<string, number>()
  for (const name of names) {
    if (name === null || slots.has(name)) continue
    if (slots.size >= TAG_SLOTS) break
    slots.set(name, slots.size)
  }
  return slots
}

/** The block's own colour, as style for the turn's outer element.
 *
 *  `undefined` for a model with no slot, so the element keeps the transparent
 *  rule `.turn` gives every block and nothing shifts by three pixels between a
 *  coloured turn and a plain one.
 *
 *  The tint is a `color-mix` of the same token rather than a fifth-and-sixth
 *  set of pale values: one token per identity is the whole ramp, and 6% is the
 *  magnitude `--hover` already uses, which keeps `--ink` body text where it
 *  was measured. */
export function tagStyle(slot: number | undefined): CSSProperties | undefined {
  if (slot === undefined) return undefined
  const hue = `var(--tag-${(slot % TAG_HUES) + 1})`
  return {
    borderLeftColor: hue,
    borderLeftStyle: slot >= TAG_HUES ? 'dashed' : 'solid',
    background: `color-mix(in srgb, ${hue} 6%, transparent)`,
  }
}

/** The label's colour: the identity itself, so the name and the rule agree
 *  without the name having to be beside the rule. Falls back to the muted ink
 *  every other label in the app uses. */
export function tagInk(slot: number | undefined): string {
  return slot === undefined ? 'var(--ink-muted)' : `var(--tag-${(slot % TAG_HUES) + 1})`
}
