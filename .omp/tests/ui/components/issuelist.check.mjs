// requires: browser
// IssueList: every variant × state renders, keyboard contract, axe clean.
// See ../lib/component.mjs for what runs; add what is specific to IssueList
// in the `keyboard` hook.
//
//   cd ui && node ../.omp/tests/ui/components/issuelist.check.mjs
import { checkComponent } from '../lib/component.mjs'

await checkComponent('IssueList')
