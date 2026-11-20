"""tests/fixtures/wiring_rules.json, read from the server's side.

The same file is read by ui/src/tabs/cluster/doc.check.mjs, which turns each
case's ``edit`` into ``ops`` through the editor's own document. Here each
case's ``ops`` go to ``POST /api/wiring`` as a dry run and must come back as
exactly its ``results`` -- so the sentences the editor shows are the ones the
server actually says, and a drift on either side fails a test.

The server is the one ``setups.default`` describes: a real ProviderService
publishing OpenRouter's three-model catalogue with two of them switched on,
and one local deployment.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.unit.test_wiring_api import _app

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "wiring_rules.json"
CASES = json.loads(FIXTURE.read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_server_answers_each_case_as_the_fixture_says(case, tmp_path):
    app, _service = _app(tmp_path)
    with TestClient(app) as client:
        reply = client.post("/api/wiring", json={"dry_run": True, "ops": case["ops"]})
    assert reply.status_code == 200, reply.text
    assert reply.json()["results"] == case["results"]
