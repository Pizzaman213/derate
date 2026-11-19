// requires: browser
// Lamp: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to Lamp.
//
//   cd ui && node ../.omp/tests/ui/components/lamp.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('Lamp')
