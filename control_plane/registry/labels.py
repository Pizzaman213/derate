"""Operator-chosen display names for nodes.

A label is a *name*, never an identity. `node_id` stays exactly what it was --
it is the key deployments, links, routing and every plan on disk are written
against, and renaming a machine must not orphan any of them. So this is a
separate field the probe never writes and the planner never reads.

The validation below is deliberately narrow. A label is rendered into an SVG
plate nine authored units tall and into an aria-label a screen reader speaks,
so a control character or a hundred-character essay is not a preference to be
honoured -- it is a rejection with a sentence saying why.
"""

from __future__ import annotations

#: Long enough for "spark-4d38 (rack 2)", short enough to fit a chip-tier
#: plate without the caption running off the edge of the machine it names.
MAX_LABEL_LEN = 48


def normalize_label(raw: object) -> str | None:
    """Return a clean label, or None to mean "no label, use the node_id".

    None and the empty string both clear it: an operator who selects the name,
    deletes it and saves has asked for the default back, and answering that
    with "a label is required" would leave them no way to undo a rename.

    Raises ValueError with an operator-facing sentence for anything else.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("A node name must be text.")
    # Collapse internal runs too: a caption is one line, and two spaces in the
    # middle of a name are invisible in the UI but not in the JSON on disk.
    label = " ".join(raw.split())
    if not label:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in label):
        raise ValueError("A node name cannot contain control characters.")
    if len(label) > MAX_LABEL_LEN:
        raise ValueError(
            f"A node name can be at most {MAX_LABEL_LEN} characters; "
            f"that one is {len(label)}."
        )
    return label
