# Speculative-decoding workloads

Three prompt sets for `python3 -m tests.spec_sweep`, and the reason there are
three rather than one.

**Acceptance rate is a property of the workload, not of the model.** ngram
drafts by matching recent output against the prompt, so it accepts most of what
it proposes when the answer largely repeats the question and almost nothing
when the answer is new prose. A single blended figure would average those into
a number that is wrong for both, and would hide the one thing that decides
whether speculation is worth turning on for *your* traffic.

| set | what it asks for | why it is here |
|---|---|---|
| `code_edit.jsonl` | a function back with a docstring, types, or error handling added | output repeats most of the input — the best case, and the one that sells the feature |
| `extraction.jsonl` | JSON over a short passage | structured output that reuses input tokens without copying them wholesale |
| `prose.jsonl` | open explanation, nothing to copy | the worst case, and the one that says "do not bother" |

**These are deliberately not generated.** `tests/load/loadtest.py::synth_prompt`
builds prompts that are unique from their first character, so that no two
requests share a prefix — exactly right for a load test, and it would drive
acceptance to near zero here and produce a measurement that means nothing.

Each line is `{"prompt": ..., "max_tokens": ...}`. `max_tokens` is fixed rather
than left to the model so every run drafts a comparable number of rounds; the
sweep reports the round count it actually got, since a set that generated fewer
tokens than expected has a noisier acceptance figure and should say so.

Adding prompts is fine and does not invalidate old records — a measurement is
stored with the workload name and the date, and the sweep is re-run rather than
merged. Changing what a set *means* is not: if `code_edit` stops being
copy-heavy it should become a new file, or every stored record under that name
starts describing a workload nobody ran.
