// requires: browser
// Nav: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to Nav.
//
//   cd ui && node ../.omp/tests/ui/components/nav.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('Nav')
