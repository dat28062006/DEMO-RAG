"""Configure verified local models, backup SQL and reindex immutable sources.
Run from mission2: python scripts/setup/setup_rag.py [--db path] [--url URL].
"""

import argparse
import json
from pathlib import Path
import sys
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.hybrid import index_document
from app.store import DEFAULT_DB, ROOT, connect, document_records, initialize


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--url", default="http://127.0.0.1:11435")
    args = parser.parse_args()
    from urllib.parse import urlsplit

    parsed = urlsplit(args.url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Only local models are supported.")
    models = json.load(urllib.request.urlopen(args.url + "/api/tags", timeout=10))["models"]
    found = {m["name"]: m for m in models}
    for name in ("qwen3-embedding:0.6b", "qwen3.5:4b"):
        if name not in found:
            raise RuntimeError("Download the required model first: " + name)
    backup_dir = ROOT / "runtime" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / ("before-hybrid-" + uuid.uuid4().hex[:8] + ".sqlite3")
    if args.db.exists():
        with connect(args.db) as src, connect(backup) as target:
            src.backup(target)
    initialize(args.db)
    rag = {
        "enabled": True,
        "url": args.url,
        "model": "qwen3-embedding:0.6b",
        "digest": found["qwen3-embedding:0.6b"]["digest"],
        "dimensions": 1024,
        "parent_similarity": 0.48,
        "max_parent_chars": 1800,
        "rrf_k": 60,
    }
    generator = {
        "enabled": True,
        "url": args.url,
        "model": "qwen3.5:4b",
        "digest": found["qwen3.5:4b"]["digest"],
        "timeout_seconds": 180,
        "max_evidence_chars": 10000,
    }
    with connect(args.db) as conn:
        for key, value in [("rag_config", rag), ("generation_config", generator)]:
            conn.execute(
                "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
        docs = document_records(conn)
    print("Backup:", backup, flush=True)
    for doc in docs:
        index = index_document(args.db, doc)
        print(doc["id"], json.dumps(index, ensure_ascii=False), flush=True)
        if index["status"] != "ready":
            raise RuntimeError("Index not ready: " + doc["id"])
    print("READY:", len(docs), "documents", flush=True)


if __name__ == "__main__":
    main()
