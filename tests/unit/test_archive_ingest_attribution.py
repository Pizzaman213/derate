"""F1: a node's journal page cannot rewrite another node's ``requests`` row.

``requests`` is the only key -> address -> model record and is not chained, so
a page from a worker carrying the coordinator's ``(request_id, attempt_no)``
must not replace it -- and the page's own ``node_id`` is never believed.
"""

from __future__ import annotations

import json

from control_plane.telemetry.archive import Archive
from control_plane.telemetry.records import KIND_REQUEST


def _page(seq: int, **body) -> dict:
    row = {"kind": KIND_REQUEST, "seq": seq, "ts": 1000.0 + seq, "body": json.dumps(body)}
    return {"rows": [row], "next": seq, "head": seq}


def _row(archive: Archive, rid: str) -> dict:
    return dict(
        archive.conn.execute(
            "SELECT node_id, key_id, client_ip, served_name FROM requests "
            "WHERE request_id=? AND attempt_no=0",
            (rid,),
        ).fetchone()
    )


def test_foreign_node_cannot_overwrite_a_request_row(tmp_path):
    archive = Archive(tmp_path / "archive.db")
    archive.ingest(
        "spark-a",
        _page(1, request_id="r-victim", attempt_no=0, served_name="m",
              key_id="realkey", client_ip="10.0.0.5"),
        trusted=True,
    )
    archive.ingest(
        "spark-b",
        _page(1, request_id="r-victim", attempt_no=0, served_name="other",
              node_id="spark-a", key_id="fakekey", client_ip="6.6.6.6"),
        trusted=True,  # even a trusted journal may not rewrite another node's row
    )
    assert _row(archive, "r-victim") == {
        "node_id": "spark-a", "key_id": "realkey", "client_ip": "10.0.0.5", "served_name": "m",
    }
    archive.close()


def test_page_node_id_is_the_pollers(tmp_path):
    archive = Archive(tmp_path / "archive.db")
    archive.ingest(
        "spark-b",
        _page(1, request_id="r-1", attempt_no=0, served_name="m", node_id="spark-a"),
        trusted=True,
    )
    assert _row(archive, "r-1")["node_id"] == "spark-b"
    archive.close()


def test_same_node_replay_still_updates(tmp_path):
    archive = Archive(tmp_path / "archive.db")
    archive.ingest("spark-a", _page(1, request_id="r-2", attempt_no=0, served_name="m", key_id="k1"), trusted=True)
    archive.ingest("spark-a", _page(2, request_id="r-2", attempt_no=0, served_name="m", key_id="k2"), trusted=True)
    assert _row(archive, "r-2")["key_id"] == "k2"
    archive.close()
