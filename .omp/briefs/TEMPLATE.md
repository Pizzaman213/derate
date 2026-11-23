# <part> brief

Owns: <one line: the job of this part>
Code: <dirs, comma separated>   Tests: <test files or dirs>   Read first: <README / CONTRACTS section>

An agent reads Depth 1 for every part it touches, and goes deeper only as far as the task needs.
Every `path::symbol` reference below is verified by the gate, so the brief cannot name code that
does not exist. Keep it to ~150 lines; link to the README for the long form instead of copying it.

## Depth 1 -- 30 seconds
At most five bullets: what this part is, where it lives, what it must never do.

## Depth 2 -- the rules
One bullet per rule that is not style: the rule, why it exists (the failure or date, not a
slogan), and where it is enforced as `path::symbol`.

## Depth 3 -- how to change it
Recipes for the changes people actually make here ("add a limit type", "add a route"): the steps
in order with `path::symbol`, the tests to add, and what to regenerate.

## Pitfalls
Things that already went wrong here: what happened, and how to see it coming.

## Functions
The entry points a newcomer must know exist, so they are reused and not rewritten. One line each:
`path::symbol` -- what it does and when to call it.
