"""The curated strip: eight shapes, and a guarantee that every one is real.

The list is the first thing on the models screen and the only thing on it
nobody chose, so a stale entry is a dead row in the most visible place in the
product. `CURATED_MODELS` used to hold four research shapes against a cap of
eight, and nothing anywhere asserted that the ids still resolved.

These are hermetic -- shape, cap and internal consistency only. Whether each
id still exists on the hub needs the network and lives in
`tests/model_sweep.py --curated`, which is what actually catches a rename.
"""

from __future__ import annotations

from control_plane.fit.catalog import CURATED_MODELS, catalog_payload
from control_plane.gateway.capacity_api import MAX_CATALOG_MODELS


def test_the_list_fits_inside_its_own_cap():
    """`/api/capacity` slices to MAX_CATALOG_MODELS, so anything past it is
    carried on this list and never answered -- a row that exists in the source
    and not on the screen."""
    assert len(CURATED_MODELS) <= MAX_CATALOG_MODELS


def test_the_cap_is_not_inert():
    """It was: four models against a cap of eight. A bound nothing approaches
    is a bound nobody is maintaining."""
    assert len(CURATED_MODELS) == MAX_CATALOG_MODELS


def test_every_id_is_distinct():
    ids = [m.model_id for m in CURATED_MODELS]
    assert len(ids) == len(set(ids))


def test_every_label_is_distinct():
    """The label is what the picker prints. Two rows reading the same is two
    rows nobody can choose between."""
    labels = [m.label for m in CURATED_MODELS]
    assert len(labels) == len(set(labels))


def test_every_entry_is_fully_populated():
    for m in CURATED_MODELS:
        assert "/" in m.model_id, f"{m.model_id} is not an owner/name repo id"
        assert m.label and m.detail
        assert m.default_context > 0
        assert m.default_concurrency > 0


def test_the_shortlist_is_not_all_the_same_kind_of_model():
    """Four MoE text generators, three of which need more than one machine,
    taught nothing about this hardware except that it refuses things. The
    strip now carries a dense model that fits on one Spark, a vision tower and
    an embedding model, and this is what stops it drifting back."""
    details = " ".join(m.detail for m in CURATED_MODELS).lower()
    for kind in ("moe", "dense", "vision", "embedding"):
        assert kind in details, f"the shortlist has nothing marked {kind}"


def test_the_payload_carries_every_field_the_picker_reads():
    payload = catalog_payload()
    assert len(payload) == len(CURATED_MODELS)
    for row in payload:
        assert set(row) >= {
            "model_id",
            "label",
            "detail",
            "default_context",
            "default_concurrency",
        }
