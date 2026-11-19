// requires: browser
// CacheTable: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to CacheTable.
//
//   cd ui && node ../.omp/tests/ui/components/cachetable.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('CacheTable')
