// requires: browser
// ProportionBar: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to ProportionBar.
//
//   cd ui && node ../.omp/tests/ui/components/proportionbar.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('ProportionBar')
