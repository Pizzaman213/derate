"""The generated manifests are current.

``manifest.json`` and ``routes.json`` are derived files, and a derived file
that is allowed to go stale is worse than no file: it answers confidently and
wrongly, which is precisely what ``00-architecture.md`` section 4.8 does today
after eleven routes shipped without being added to it.

Nothing here restates a contract. Both tests regenerate from the live source
and compare, so the only thing they can catch is somebody changing a contract
and not regenerating -- and the only way to fix a failure is to run the command
the failure message names.
"""

from __future__ import annotations

import json

from control_plane.contracts import document, manifest, routes


def _fix(command: str) -> str:
    return (
        f"\n\nThe generated file is stale. Regenerate it:\n\n    {command}\n\n"
        "Then read the diff before committing -- it is the list of contracts "
        "your change moved."
    )


def test_the_contract_manifest_matches_the_contracts():
    checked_in = json.loads(manifest.MANIFEST_PATH.read_text(encoding="utf-8"))
    live = manifest.build()
    assert checked_in == live, _fix("python3 -m control_plane.contracts.manifest --write")


def test_the_route_manifest_matches_the_apps():
    checked_in = json.loads(routes.ROUTES_PATH.read_text(encoding="utf-8"))
    live = routes.build()
    assert checked_in == live, _fix("python3 -m control_plane.contracts.routes --write")


def test_the_generated_contract_document_matches_the_manifests():
    checked_in = document.DOCUMENT_PATH.read_text(encoding="utf-8")
    assert checked_in == document.render(), _fix(
        "python3 -m control_plane.contracts.document --write"
    )


def test_the_generated_document_is_not_a_second_place_to_look():
    """CONTRACTS.md must say every route the apps answer.

    Its whole reason for existing is that the hand-written table in
    ``00-architecture.md`` section 4.8 fell seventeen product routes behind
    while still introducing itself as the one the UI codes against.
    """
    text = document.render()
    for path in ("/api/setup", "/api/models/search", "/api/nodes/{node_id}/runtime"):
        assert path in text, f"{path} answers requests but the document omits it"


def test_the_manifest_reflects_rather_than_restates():
    """A guard on the generator itself.

    If ``reflect_contracts`` ever stopped finding types -- an import moved, the
    package layout changed -- both tests above would still pass, comparing an
    empty manifest against an empty manifest. So assert it found the things
    every component agrees on.
    """
    live = manifest.build()
    for name in ("NodeProfile", "ModelShape", "ParallelismPlan", "DeviceClass", "Verdict"):
        assert name in live["types"], f"{name} vanished from the manifest"
    assert live["types"]["DeviceClass"]["members"]["GB10"] == "gb10"
    assert "GB10_TOTAL_MEMORY" in live["constants"]
    assert live["derived"], "no derived facts resolved"
    assert len(live["env"]) > 40, "the environment table is suspiciously short"


def test_every_route_the_gateway_answers_is_listed():
    """The generated table is the whole surface, both apps.

    Guards the same failure as above from the other side: FastAPI now wraps an
    included router rather than copying its routes onto the app, so a collector
    reading only ``app.routes`` sees four routes and reports the rest do not
    exist.
    """
    live = routes.build()
    paths = {r["path"] for r in live["gateway"]}
    for path in ("/api/nodes", "/api/deployments", "/api/settings", "/v1/chat/completions"):
        assert path in paths, f"{path} is missing; the collector is not descending into routers"
    assert len(live["gateway"]) > 50, f"only {len(live['gateway'])} gateway routes collected"
    assert {r["path"] for r in live["agent"]}, "the node agent surface is empty"
