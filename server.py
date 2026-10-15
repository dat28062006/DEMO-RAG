"""Run: python server.py --port 8765. Binds only to loopback."""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.engine import KnowledgeEngine
from app.control import ControlService
from app.importer import import_documents
from app.file_import import import_file, original_file
from app.store import DEFAULT_DB, ROOT, initialize
from app import llm_api

STATIC = ROOT / "app" / "static"
LOGGER = logging.getLogger("mission2")


def handler_for(engine: KnowledgeEngine):
    control = ControlService(engine.db_path)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def send_data(self, status, data, content_type="application/json; charset=utf-8"):
            if isinstance(data, (dict, list)):
                data = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/api/llm-settings":
                try:
                    return self.send_data(
                        200,
                        llm_api.status(engine.db_path, parse_qs(url.query).get("user_id", [""])[0]),
                    )
                except PermissionError as exc:
                    return self.send_data(403, {"error": str(exc)})
            if url.path.endswith("/file") and url.path.startswith(
                ("/api/sources/", "/api/review/documents/")
            ):
                review = url.path.startswith("/api/review/")
                prefix = "/api/review/documents/" if review else "/api/sources/"
                sid = url.path.removeprefix(prefix).removesuffix("/file")
                uid = parse_qs(url.query).get("user_id", [""])[0]
                original = original_file(engine.db_path, uid, sid, review=review)
                if not original:
                    return self.send_data(
                        404, {"error": "File gốc không khả dụng với bối cảnh hiện tại."}
                    )
                return self.send_data(200, original["original"], original["media_type"])
            if url.path.startswith("/api/review/") or url.path == "/api/reports":
                uid = parse_qs(url.query).get("user_id", [""])[0]
                try:
                    if url.path == "/api/review/documents":
                        return self.send_data(200, control.catalogue(uid))
                    if url.path.startswith("/api/review/documents/"):
                        return self.send_data(
                            200,
                            control.detail(uid, url.path.removeprefix("/api/review/documents/")),
                        )
                    if url.path == "/api/reports":
                        return self.send_data(200, control.feedback_list(uid))
                except PermissionError as exc:
                    return self.send_data(403, {"error": str(exc)})
                return self.send_data(404, {"error": "Không tìm thấy tài nguyên."})
            if url.path == "/api/health":
                from app.hybrid import config_for
                from app.generation import generation_config

                rag = config_for(engine.db_path)
                generator = generation_config(engine.db_path)
                return self.send_data(
                    200,
                    {
                        "status": "ok",
                        "mode": "hybrid_rag" if rag and rag.get("enabled") else "extractive",
                        "demo_identity": True,
                        "models": {
                            "embedding": rag.get("model") if rag else None,
                            "generation": generator.get("model") if generator else None,
                        },
                    },
                )
            if url.path == "/api/bootstrap":
                return self.send_data(200, engine.bootstrap())
            if url.path == "/api/library":
                try:
                    params = parse_qs(url.query)
                    return self.send_data(
                        200,
                        engine.library(
                            params.get("user_id", [""])[0], params.get("as_of", [None])[0]
                        ),
                    )
                except PermissionError as exc:
                    return self.send_data(403, {"error": str(exc)})
                except ValueError as exc:
                    return self.send_data(400, {"error": str(exc)})
            if url.path.startswith("/api/sources/"):
                source_id = url.path.removeprefix("/api/sources/")
                params = parse_qs(url.query)
                user_id = params.get("user_id", [""])[0]
                request_id = params.get("request_id", [None])[0]
                index = params.get("citation_index", [None])[0]
                if request_id is not None and (
                    not re.fullmatch(r"[0-9a-f]{32}", request_id)
                    or index is None
                    or not re.fullmatch(r"[0-9]{1,4}", index)
                ):
                    return self.send_data(
                        404, {"error": "Nguồn không khả dụng với bối cảnh hiện tại."}
                    )
                source = engine.source(
                    user_id,
                    source_id,
                    request_id,
                    int(index) if index and index.isdigit() else None,
                    passage_only=params.get("view", [None])[0] == "passage",
                )
                return (
                    self.send_data(200, source)
                    if source
                    else self.send_data(
                        404, {"error": "Nguồn không khả dụng với bối cảnh hiện tại."}
                    )
                )
            files = {
                "/": "index.html",
                "/index.html": "index.html",
                "/app.js": "app.js",
                "/styles.css": "styles.css",
                "/favicon.svg": "favicon.svg",
            }
            if url.path not in files:
                return self.send_data(404, {"error": "Không tìm thấy tài nguyên."})
            path = STATIC / files[url.path]
            if not path.is_file():
                return self.send_data(404, {"error": "Tài nguyên chưa sẵn sàng."})
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_data(
                200,
                path.read_bytes(),
                mime
                + ("; charset=utf-8" if mime.startswith("text/") or "javascript" in mime else ""),
            )

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in (
                "/api/ask",
                "/api/feedback",
                "/api/import",
                "/api/import-file",
                "/api/llm-settings",
            ) and not path.startswith(("/api/review/documents/", "/api/reports/")):
                return self.send_data(404, {"error": "Không tìm thấy tài nguyên."})
            # Reject cross-site browser requests; the demo is intentionally bound to localhost.
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                return self.send_data(403, {"error": "Nguồn yêu cầu không hợp lệ."})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > (3000000 if path == "/api/import-file" else 128000):
                    raise ValueError("Kích thước request không hợp lệ.")
                payload = json.loads(self.rfile.read(size).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Payload phải là object JSON.")
                if path == "/api/llm-settings":
                    return self.send_data(200, llm_api.configure(engine.db_path, payload))
                if path == "/api/feedback":
                    return self.send_data(200, control.submit_feedback(payload))
                if path == "/api/import":
                    return self.send_data(200, import_documents(engine.db_path, payload))
                if path == "/api/import-file":
                    return self.send_data(200, import_file(engine.db_path, payload))
                if path.startswith("/api/review/documents/"):
                    return self.send_data(
                        200, control.update(path.removeprefix("/api/review/documents/"), payload)
                    )
                if path.startswith("/api/reports/"):
                    return self.send_data(
                        200, control.feedback_status(path.removeprefix("/api/reports/"), payload)
                    )
                response = engine.answer(payload)
                LOGGER.info(
                    "request=%s user=%s status=%s mode=%s",
                    response["request_id"],
                    response["user"]["id"],
                    response["status"],
                    response["mode"],
                )
                return self.send_data(200, response)
            except PermissionError as exc:
                return self.send_data(403, {"error": str(exc)})
            except (ValueError, UnicodeError) as exc:
                return self.send_data(400, {"error": str(exc)})
            except Exception:
                LOGGER.exception("Unexpected request failure")
                return self.send_data(
                    500, {"error": "Lỗi xử lý. Kiểm tra log server; chưa phát kết luận."}
                )

        def log_message(self, format, *args):
            LOGGER.info(format, *args)

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--reset", action="store_true", help="Reset only the selected demo database to fixtures"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    initialize(args.db, reset=args.reset)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(KnowledgeEngine(args.db)))
    from app.hybrid import config_for

    configured = config_for(args.db)
    print(
        f"Mission 2 local: http://127.0.0.1:{args.port}\nMode: {'hybrid_rag' if configured and configured.get('enabled') else 'extractive'} | Dữ liệu và danh tính giả lập",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
