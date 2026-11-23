# Starter prompt: design something for derate

Paste this (or point an agent at this file) at the start of any design or non-trivial task.

> You are designing a change for derate. Work in layers and stop as soon as you know enough.
>
> 1. Read `.omp/briefs/README.md` (one page: what derate is, six principles, the parts).
> 2. For each part the task touches, read only **Depth 1** of its brief in `.omp/briefs/`. State in
>    one sentence per part what it owns and what it must never do.
> 3. Read **Depth 2** (the rules) for the part you will change, and **Depth 3** for the recipe closest
>    to the task. Read **Pitfalls** before you write the plan.
> 4. Run `node .omp/scripts/functions.mjs <verb>` for every helper you expect to write, and
>    `node .omp/scripts/ownership.mjs --headroom <files>` for every file you expect to grow.
> 5. Write the design note (`.omp/design-note.md` template) and cite the briefs and `path::symbol`
>    you rely on. Ask `design-critic` to review it. Only then write code.
> 6. If the task contradicts a rule in a brief, stop and say so; do not route around it.
>
> Report which briefs you read and to what depth, so the reader can see the design rests on them.
