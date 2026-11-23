# Design note: <short title>

File as `.omp/work/design-notes/<slug>.md` BEFORE writing code, in the same change as the code.
`design-critic` reads it; do not implement until it approves or you have answered its objections
in the note. Keep it to one page. Delete sections that do not apply, never leave them empty.

## Ask
What the user asked for, in their words, and the acceptance criteria.

## Where it goes
- Owning area(s), from `.omp/OWNERSHIP.md`, and why that one.
- Files that will change, with `node .omp/scripts/ownership.mjs --headroom <files>` output.
- New files, and what each one is the single home of.

## What already exists
Existing code, constants or routes that do part of this, and whether you reuse, extend or replace
it. Cite `file:line`. "Nothing" needs the searches that found nothing.

## Decisions
Each non-obvious choice, the alternative you rejected and why. These are what a future reader
will otherwise reopen.

## Size and risk
Net lines expected per file; anything that will cross its budget and the split that avoids it;
what could break (data files, public contracts, other sessions' work in the tree).

## Verification
The exact commands and the observable result that proves it works.
