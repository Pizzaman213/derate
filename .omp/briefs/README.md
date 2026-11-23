# How to design something for derate

Start here. Read this page, then the Depth 1 of each brief for the parts your task touches, and go
deeper only as far as the task needs. Do not read the whole repo, and do not start from
`CLAUDE.md` (it is the long form; each brief points at the sections that matter).

## What derate is
Planning and orchestration for DGX Spark clusters. It **measures** the interconnect, **derives**
the parallelism plan from what was measured, **refuses** launches that will run out of memory, and
**fronts** every model behind one OpenAI endpoint (`/v1`). One process (`control_plane/node.py`)
takes its role at runtime; the UI is a screen over `/api`.

## Six principles that decide most design questions
1. **Measured beats assumed, and unmeasured is said out loud.** A missing number is shown as
   missing (an em dash, "no reading"), never as zero and never as a guess. Constants that stand in
   for a measurement say so on screen.
2. **A refusal names what to change.** Planner and fit strings are the product; they reach the
   screen verbatim (`Verbatim`), never truncated, re-cased or summarised.
3. **One home per fact.** A rule, constant or shape lives in one place (`contracts/`,
   `contracts/derived.py`) and every copy is derived or tested against it
   (`tests/unit/test_single_source.py`). Contracts change in one place, announced.
4. **What is shown and what is sent are one value.** If a control displays a number computed at
   one setting, it applies that setting. If a gate priced something, the launch carries it, or the
   launch is refused; never dropped in silence.
5. **Gated by default.** A new route is behind the credential gate (`gateway/authz.py`) unless
   deliberately opened in one hardcoded list. Secrets are shown once and never rendered again.
6. **Degrade, never fabricate.** Live memory falls back to the static ceiling; a CPU node with no
   measurement refuses rather than inventing a budget.

## The parts (read the brief for each part you touch)
| part | brief | owns |
|---|---|---|
| contracts, keys, auth, limits | `contracts-keys-auth.md` | the frozen shapes; orgs, `derate-` keys, admin sessions, the audit log; rate limits and quotas |
| gateway, providers | `gateway-providers.md` | the one HTTP surface (`/v1`, `/api`, the built UI); remote upstreams as route targets |
| resolver, fit, planner | `fit-planner-resolver.md` | HuggingFace id -> model shape; the memory gate; TP/PP/EP from measured facts |
| deploy, runtimes, registry, links | `deploy-registry-links.md` | turning a plan into a running backend; nodes and their health; measured interconnect |
| the UI | `ui.md` | `ui/`: URL scheme, verifiers, the design pack, redaction, the screens |

## The workflow
1. **Place it.** `.omp/OWNERSHIP.md` says which area owns the concern; the brief says how that
   area expects to be changed.
2. **Find what exists.** `node .omp/scripts/functions.mjs <verb> [<noun>]` before writing any
   helper; `--in <dir>` to scope. If something close exists, extend it or say why not.
3. **Check room.** `node .omp/scripts/ownership.mjs --headroom <files>`; a file with little room
   left is where the change should not go.
4. **Write the design note** for anything large (`.omp/design-note.md`), citing the briefs you used
   and the `path::symbol` you will reuse; get `design-critic`'s verdict before coding.
5. **Build, then validate** (`cd .omp && npm run validate`) and run the reviewers in one parallel
   batch (`.omp/rules/validation.md`).

## Keeping the briefs true
A brief that is wrong is worse than none. When a change alters a rule, a recipe or an entry point
named in a brief, update the brief in the same change. The gate verifies every `path::symbol` still
exists; whether the prose is still right is for `design-critic` and the reviewers to judge.
Template: `TEMPLATE.md`.
