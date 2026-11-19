// requires: browser
// Verbatim / VerbatimList: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to Verbatim / VerbatimList.
//
//   cd ui && node ../.omp/tests/ui/components/verbatim.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('Verbatim')
await checkComponent('VerbatimList')
