"""Build a disposable demo template, or serve its writable copy on a cloud host.

No developer database, uploaded files or API credentials are included in the image.
"""

import argparse
import json
import logging
import os
import shutil
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import llm_api
from app.engine import KnowledgeEngine
from app.hybrid import config_for, encoder_for, index_document
from app.onnx_encoder import MODEL, REVISION, prepare_model
from app.store import ROOT, connect, document_records, initialize
from server import handler_for, validate_public_origin

LOGGER = logging.getLogger("rag.cloud")


def prepare(db, model_directory):
    """Index fake fixtures at image build time; never reset an existing database."""
    digest = prepare_model(model_directory)
    initialize(db)
    config = {
        "enabled": True,
        "provider": "onnx",
        "model": MODEL,
        "model_revision": REVISION,
        "model_directory": str(Path(model_directory).resolve()),
        "digest": digest,
        "dimensions": 384,
        "parent_similarity": 0.48,
        "max_parent_chars": 1800,
        "rrf_k": 60,
    }
    with connect(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings VALUES('rag_config',?)", (json.dumps(config),)
        )
        docs = document_records(conn)
    for doc in docs:
        manifest = index_document(db, doc)
        if manifest["status"] != "ready":
            raise RuntimeError("Cloud index preparation failed: " + doc["id"])
    LOGGER.info("Cloud template ready: %s indexed documents", len(docs))


def copy_template(template, db):
    """Reuse runtime data on restart when storage exists; do not overwrite it."""
    db = Path(db)
    if db.exists():
        return False
    db.parent.mkdir(parents=True, exist_ok=True)
    with Path(template).open("rb") as source, db.open("xb") as target:
        shutil.copyfileobj(source, target)
    return True


def configure_secret(db, environment):
    """Move the hosting secret into the existing RAM credential store."""
    secret = environment.pop("GROQ_API_KEY", "").strip()
    if not secret:
        LOGGER.warning("No Groq secret configured; enter it via the LLM settings UI.")
        return False
    try:
        llm_api.configure(db, {
            "user_id": "U04", "provider": "groq", "consent": True,
            "model": environment.get("GROQ_MODEL", "openai/gpt-oss-120b"),
            "api_key": secret,
        })
    except ValueError as exc:
        # configure() sanitizes provider errors. Never log the payload or secret.
        LOGGER.warning("Groq activation failed: %s", exc)
        return False
    return True


def cloud_handler(engine, origin):
    base = handler_for(engine, origin)
    mutation = threading.BoundedSemaphore(1)

    class CloudHandler(base):
        def do_POST(self):
            if not mutation.acquire(blocking=False):
                self.close_connection = True
                return self.send_data(503, {
                    "error": "Demo đang xử lý một yêu cầu khác. Vui lòng thử lại sau ít giây."
                })
            try:
                return super().do_POST()
            finally:
                mutation.release()

    return CloudHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="Build the seed index; no API key needed")
    parser.add_argument("--template", type=Path, default=ROOT / "cloud-assets" / "template.sqlite3")
    parser.add_argument("--model-directory", type=Path, default=ROOT / "cloud-assets" / "model")
    parser.add_argument("--db", type=Path, default=ROOT / "runtime" / "cloud.sqlite3")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "10000")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.prepare:
        return prepare(args.template, args.model_directory)
    origin = validate_public_origin(
        os.environ.get("PUBLIC_ORIGIN") or os.environ.get("RENDER_EXTERNAL_URL") or None
    )
    copy_template(args.template, args.db)
    initialize(args.db)
    config = config_for(args.db)
    if not config or config.get("provider") != "onnx":
        raise RuntimeError("Run cloud.py --prepare first to build the cloud template.")
    encoder_for(config).verify_model()
    configure_secret(args.db, os.environ)
    server = ThreadingHTTPServer(
        (args.host, args.port), cloud_handler(KnowledgeEngine(args.db), origin)
    )
    LOGGER.info("DEMO RAG listening on %s:%s (disposable cloud demo)", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
