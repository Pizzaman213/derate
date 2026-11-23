# Lessons

Every finding a reviewer or a user had to make by hand, and what now stops it. The reviewers read
this first, so they spend their judgement on what a script cannot check. `enforced by` is the exact
name of a gate step in `scripts/validate/*.mjs`, `reviewer:<agent>` when only judgement can catch
it, or `todo` when it should become a gate step and is not one yet. The gate fails if a name here
is not a real step. When a reviewer reports something mechanical twice, add a row and a step.

| lesson | seen | enforced by |
|---|---|---|
| A scratch coordinator on a host with model containers adopts them and relaunches them with its own API key | 2026-11-23, breaker | reviewer:breaker (agent forbidden to start one; CLAUDE.md names the safe flags) |
| A file crosses its size budget inside the change that grew it, so a ratchet recorded afterwards passes it | 2026-11-23, repo-steward | files stay within their line budget |
| A large change is written before anyone asks where it should live or what it duplicates | 2026-11-23, repo-steward, duplication-reviewer | a large change has a design note (.omp/work/design-notes/) |
| A new area appears without a line saying what it owns, so the next change lands in the wrong place | 2026-11-23 | ownership map lists every area; changed areas say what they are for |
| README file tables miss new files and keep old line counts | 2026-11-23, design-reviewer | README file tables list new files and follow big changes |
| A native `<select>` where the themed `Select` exists | 2026-11-23, user | no native <select> (use components/Select) |
| A new top-level function already defined in another file | 2026-11-23, duplication-reviewer | new functions do not already exist in another file |
| A response built from a whole record leaks fields added later (`/v1/key` showed profile ids) | 2026-11-23, security-reviewer | reviewer:security-reviewer |
| The same SQL, rule or lookup spelled twice in one module | 2026-11-23, repo-steward | reviewer:duplication-reviewer |
| A second subscription to the same endpoint on one screen | 2026-11-23, duplication-reviewer | todo |
| A hand-kept route list in a test that a generated coverage test already walks | 2026-11-23, duplication-reviewer | todo |
| Zero-context patches applied to files other sessions are editing land in the wrong place | 2026-11-23 | todo (task-start/task-end scripts) |
