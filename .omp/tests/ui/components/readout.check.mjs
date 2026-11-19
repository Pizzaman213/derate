// requires: browser
// Readout: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to Readout.
//
//   cd ui && node ../.omp/tests/ui/components/readout.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('Readout')
