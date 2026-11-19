// requires: browser
// Table: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; this file adds only what is
// specific to Table.
//
//   cd ui && node ../.omp/tests/ui/components/table.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('Table')
