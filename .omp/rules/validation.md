---
description: Every task ends with the validation gate and independent review
alwaysApply: true
---
Before STARTING any non-trivial task (more than a one-file edit):

0. Understand derate before designing for it. Read `.omp/briefs/README.md` (one page), then Depth 1
   of the brief for each part the task touches; go to Depth 2/3 only as far as needed
   (`.omp/prompts/design-for-derate.md` is the starter prompt). Then decide where the code goes:
   read `.omp/OWNERSHIP.md` (which area owns the concern) and
   `.omp/lessons.md` (what has gone wrong before), and run
   `node .omp/scripts/functions.mjs <verb>` for every helper you expect to write (reuse what exists) and
   `node .omp/scripts/ownership.mjs --headroom <files you expect to change>`. A change over
   ~150 net new source lines, or any growth in a file past 70% of its budget, needs a design
   note first: copy `.omp/design-note.md` to `.omp/work/design-notes/<slug>.md`, fill it in,
   and get the `design-critic` agent's verdict before writing code. The gate refuses such a
   change with no note. When you add an area (a new package or tab directory), give it a
   README whose first paragraph says what it owns and re-run `node .omp/scripts/ownership.mjs`.

Before reporting ANY task done (anything beyond a one-line or docs-only change):

1. Run `cd .omp && npm run validate`. It is the mechanical gate; the step names it prints say
   what it checks (line budgets, generated-file staleness, typecheck and UI verifiers,
   native `<select>`, literal colours, inline SVG, missing tests, duplicate functions).
   Red means not done. UI verifiers run `--strict` and need `DERATE_CHECK_ADMIN_USER` /
   `DERATE_CHECK_ADMIN_PASSWORD`; `VALIDATE_LENIENT=1` is for a machine with no coordinator
   and must be said out loud in the report.
2. At the END of the task, spawn ALL the reviewers below in ONE parallel `task` batch (never
   one after another), on the diff (`git diff $(git merge-base HEAD dev)` plus
   untracked files), giving each the user's acceptance criteria and NOT your reasoning:
   - `duplication-reviewer` -- ALWAYS, for every task: finds new code that overlaps code
     that already exists.
   - `repo-steward` -- ALWAYS, for every task: long-term maintainability (file, script and
     function size against the budgets in `.omp/line-budget.json`, scripts doing too many
     jobs, files that keep growing). Its size numbers come from the gate's line-budget step.
   - `breaker` -- ALWAYS, for every task: runs the change and tries to break it (hostile
     input, wrong order, stale state, browser behaviour). Read-only on tracked files.
   - `design-reviewer` -- when `ui/src` or `.omp/design` changed.
   - `security-reviewer` -- when auth, keys, sessions, gateway routes or contracts changed.
   - `reviewer` -- anything else non-trivial.
3. Reviewers read `.omp/lessons.md` first. When a finding is mechanical (a script could have
   caught it), add a row to `lessons.md` and, if it recurs, a gate step.
4. Fix what they find, then re-run the gate (and only the reviewers whose area you changed).
   Every finding is fixed or answered in the final report with a reason. Say which
   reviewers ran and what each found, including "no findings".

The pre-push hook in `.githooks/` runs step 1; step 2 is on you.

## Every agent, not only the main one

This applies to every agent that edits files: the main session, `task`, `sonic`, `icon-smith`
and anything a task spawns. Before such an agent returns to its caller with "done":

- run `cd .omp && npm run validate -- --fast` (the full run when the work touched Python,
  contracts or the UI build) and paste the PASS/FAIL lines into its result;
- never report done on a red gate: fix it, or return the red lines as the result and say
  what is blocking;
- an agent that only reads (`scout`, `reviewer`, `design-reviewer`, `security-reviewer`,
  `duplication-reviewer`, `repo-steward`, `breaker`) is exempt, because it has no change to
  validate. `security-reviewer` and `reviewer` are omp built-ins and have no file in `.omp/agents/`.

The agent that started the work still runs step 2 (the parallel reviewers) once, at the end;
subagents do not each spawn their own.
