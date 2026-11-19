// requires: browser
// Chip: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to Chip.
//
//   cd ui && node ../.omp/tests/ui/components/chip.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('Chip')
