// requires: browser
// Card: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to Card.
//
//   cd ui && node ../.omp/tests/ui/components/card.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('Card')
