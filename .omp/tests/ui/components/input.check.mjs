// requires: browser
// Input: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to Input.
//
//   cd ui && node ../.omp/tests/ui/components/input.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('Input')
