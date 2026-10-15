"""Adopt an evaluated disposable index without changing source documents or rights."""

import argparse
from array import array
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.hybrid import config_for, config_hash, REVISION
from app.store import DEFAULT_DB, connect


def adopt(prepared_db, live_db=DEFAULT_DB):
    prepared_db, live_db = Path(prepared_db).resolve(), Path(live_db).resolve()
    if prepared_db == live_db or not prepared_db.is_file() or not live_db.is_file():
        raise ValueError("Need distinct existing prepared and live databases.")
    config = config_for(live_db)
    expected = config_hash(config)
    if config_hash(config_for(prepared_db)) != expected:
        raise ValueError("Prepared embedding configuration differs from live configuration.")
    with connect(prepared_db) as source:
        documents = {
            r["id"]: dict(r)
            for r in source.execute("SELECT id,checksum,section_text FROM documents")
        }
        manifests = {r["document_id"]: dict(r) for r in source.execute("SELECT * FROM rag_indexes")}
        parents = [dict(r) for r in source.execute("SELECT * FROM rag_parents")]
        children = [dict(r) for r in source.execute("SELECT * FROM rag_children")]
    for sid, doc in documents.items():
        manifest = manifests.get(sid)
        if (
            not manifest
            or manifest["status"] != "ready"
            or manifest["config_hash"] != expected
            or manifest["source_checksum"] != doc["checksum"]
            or manifest["model"] != config["model"]
            or manifest["model_digest"] != config["digest"]
            or manifest["dimensions"] != config["dimensions"]
        ):
            raise ValueError("Prepared index is incomplete or stale: " + sid)
    for row in parents:
        doc = documents[row["document_id"]]
        if (
            row["source_checksum"] != doc["checksum"]
            or doc["section_text"][row["start_offset"] : row["end_offset"]] != row["text"]
        ):
            raise ValueError("Prepared parent does not match original text.")
    by_parent = {row["id"]: row for row in parents}
    for row in children:
        doc = documents[row["document_id"]]
        if (
            doc["section_text"][row["start_offset"] : row["end_offset"]] != row["text"]
            or len(row["embedding"]) != 4 * config["dimensions"]
        ):
            raise ValueError("Prepared child or vector is invalid.")
        parent = by_parent.get(row["parent_id"])
        values = array("f", row["embedding"])
        if (
            not parent
            or parent["document_id"] != row["document_id"]
            or not parent["start_offset"]
            <= row["start_offset"]
            < row["end_offset"]
            <= parent["end_offset"]
            or any(not math.isfinite(value) for value in values)
            or abs(sum(value * value for value in values) - 1) > 0.01
        ):
            raise ValueError("Prepared child parent or vector normalization is invalid.")
    for sid, manifest in manifests.items():
        if manifest["parent_count"] != sum(r["document_id"] == sid for r in parents) or manifest[
            "child_count"
        ] != sum(r["document_id"] == sid for r in children):
            raise ValueError("Prepared index counts are inconsistent.")
    backup = (
        live_db.parent
        / "backups"
        / ("before-index-" + REVISION + "-" + uuid.uuid4().hex[:8] + ".sqlite3")
    )
    backup.parent.mkdir(parents=True, exist_ok=True)
    with connect(live_db) as live, connect(backup) as saved:
        live.backup(saved)
    with connect(live_db) as live:
        live.execute("BEGIN IMMEDIATE")
        current = {
            r["id"]: dict(r) for r in live.execute("SELECT id,checksum,section_text FROM documents")
        }
        if current != documents or config_hash(config_for(live_db)) != expected:
            raise ValueError("Sources or configuration changed since audit; nothing adopted.")
        for table in ("rag_children", "rag_parents", "rag_indexes"):
            live.execute("DELETE FROM " + table)
        for table, rows in [
            ("rag_indexes", list(manifests.values())),
            ("rag_parents", parents),
            ("rag_children", children),
        ]:
            columns = [r[1] for r in live.execute("PRAGMA table_info(" + table + ")")]
            statement = (
                "INSERT INTO "
                + table
                + "("
                + ",".join(columns)
                + ") VALUES("
                + ",".join("?" for _ in columns)
                + ")"
            )
            live.executemany(statement, [tuple(row[column] for column in columns) for row in rows])
        live.execute(
            "INSERT INTO audit(actor_id,document_id,action,occurred_at,detail_json) VALUES(?,?,?,?,?)",
            (
                "SYSTEM",
                "SYSTEM",
                "system.reindex",
                datetime.now(timezone.utc).isoformat(),
                json.dumps(
                    {"revision": REVISION, "documents": len(documents), "backup": str(backup)}
                ),
            ),
        )
    return {
        "backup": str(backup),
        "revision": REVISION,
        "documents": len(documents),
        "parents": len(parents),
        "children": len(children),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepared-db", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    print(json.dumps(adopt(args.prepared_db, args.db), ensure_ascii=True))
