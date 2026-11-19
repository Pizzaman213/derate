// requires: browser
// BlockFrame: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; add what is specific to BlockFrame
// in the `keyboard` hook.
//
//   cd ui && node ../.omp/tests/ui/components/blockframe.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('BlockFrame')
