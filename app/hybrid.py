"""Local dense + BM25 retrieval on sentence children, with semantic parent expansion.

The model is an embedding encoder, not a generator or an authorization service.
SQLite retains the canonical version/checksum and the disposable index manifest.
"""

from __future__ import annotations

from array import array
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import threading
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .store import connect

REVISION = "semantic-sentence-parent-v4"
QUERY_INSTRUCTION = "Given a Vietnamese question, retrieve relevant sentences from internal documents that answer the question."
_encoder_lock = threading.Lock()
HEADINGS = re.compile(
    r"(?m)^(?:#{1,6}[ \t]+\S[^\n]*|\d+(?:\.\d+)*[.)][ \t]+\S[^\n]{2,110}|\d{1,2}(?:\.\d{1,2})*[ \t]+[A-ZĐ][^\n]{1,99}|References|Bibliography|Tài liệu tham khảo|[^\n]{2,100}/\s*\d{2}\s*\n(?P<page_title>[^\n]{3,110}))$"
)
STOP = set(
    "tôi bạn của cho là có và nếu thì một các bao nhiêu nào khi theo với những này đó trong từ đến sau hỏi hãy giúp công ty tài liệu quy định".split()
)


class IndexUnavailable(RuntimeError):
    pass


def normalized(text):
    return unicodedata.normalize("NFKC", text).casefold()


def lexical_terms(text):
    return [w for w in re.findall(r"[^\W_]+", normalized(text)) if w not in STOP]


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def config_for(db):
    with connect(db) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='rag_config'").fetchone()
    return json.loads(row[0]) if row else None


def config_hash(config):
    return canonical_hash(
        {**config, "splitter_revision": REVISION, "query_instruction": QUERY_INSTRUCTION}
    )


def unit_vector(vector, dimensions):
    if len(vector) != dimensions or any(
        not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector
    ):
        raise IndexUnavailable("Embedding có số chiều hoặc giá trị không hợp lệ.")
    length = math.sqrt(sum(v * v for v in vector))
    if length < 1e-12:
        raise IndexUnavailable("Embedding không được là vector rỗng.")
    return [v / length for v in vector]


def cosine(a, b):
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))


class OllamaEncoder:
    def __init__(self, config):
        self.config = config
        self.url = config["url"].rstrip("/")
        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.username
            or parsed.password
        ):
            raise IndexUnavailable("MVP chỉ cho phép dịch vụ embedding loopback đã cấu hình.")

    def request(self, path, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        req = urllib.request.Request(
            self.url + path, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                return json.load(response)
        except (OSError, ValueError, urllib.error.URLError):
            raise IndexUnavailable(
                "Dịch vụ embedding local chưa sẵn sàng; chưa có kết quả hybrid đầy đủ."
            ) from None

    def verify_model(self):
        models = self.request("/api/tags").get("models", [])
        match = next((m for m in models if m["name"] == self.config["model"]), None)
        if not match or match.get("digest") != self.config["digest"]:
            raise IndexUnavailable(
                "Model embedding khác revision đã khóa; cần xác minh và reindex."
            )

    def embed(self, texts, query=False):
        if not texts:
            return []
        self.verify_model()
        inputs = [normalized(t) for t in texts]
        if query:
            inputs = [f"Instruct: {QUERY_INSTRUCTION}\nQuery: {t}" for t in inputs]
        output = []
        with _encoder_lock:
            for offset in range(0, len(inputs), 32):
                data = self.request(
                    "/api/embed",
                    {
                        "model": self.config["model"],
                        "input": inputs[offset : offset + 32],
                        "dimensions": self.config["dimensions"],
                        "truncate": False,
                        "keep_alive": "30m",
                        "options": {"num_ctx": 2048, "num_thread": 4},
                    },
                )
                vectors = data.get("embeddings", [])
                if len(vectors) != len(inputs[offset : offset + 32]):
                    raise IndexUnavailable(
                        "Embedding chưa trả đủ các câu; không công bố chỉ mục một phần."
                    )
                output.extend(unit_vector(v, self.config["dimensions"]) for v in vectors)
        return output


def sentences(text):
    """Keep exact source offsets; punctuation between digits is not a boundary."""
    headings = list(HEADINGS.finditer(text))
    sections = sorted({0, len(text), *[h.start() for h in headings]})
    result = []
    for left, right in zip(sections, sections[1:]):
        section_heading = next((h for h in reversed(headings) if h.start() <= left), None)
        heading = (
            (section_heading.group("page_title") or section_heading.group()).lstrip("# ")
            if section_heading
            else "Nội dung"
        )
        leading = HEADINGS.match(text, left)
        body_start = leading.end() if leading else left
        if leading:
            begin = leading.start("page_title") if leading.group("page_title") else leading.start()
            result.append(
                {
                    "text": text[begin : leading.end()],
                    "start": begin,
                    "end": leading.end(),
                    "heading": heading,
                    "block": (left, body_start),
                    "line_start": text.count("\n", 0, begin) + 1,
                    "line_end": text.count("\n", 0, leading.end()) + 1,
                }
            )
        # Paragraph matches are relative to body_start.
        paragraph_starts = [
            body_start,
            *[body_start + m.end() for m in re.finditer(r"\n\s*\n", text[body_start:right])],
            right,
        ]
        for pstart, pend in zip(paragraph_starts, paragraph_starts[1:]):
            start = pstart
            boundaries = []
            for m in re.finditer(r"[.!?](?=\s|$)", text[pstart:pend]):
                pos = pstart + m.start()
                if (
                    text[pos] == "."
                    and pos > 0
                    and text[pos - 1].isdigit()
                    and pos + 1 < len(text)
                    and text[pos + 1].isdigit()
                ):
                    continue
                token = re.search(r"\w+$", text[start:pos])
                if text[pos] == "." and re.search(
                    r"\b(?:w\.r\.t|e\.g|i\.e|etc|fig|eq|dr|mr|ms|prof|al)$",
                    text[start:pos],
                    re.I,
                ):
                    continue
                if (
                    text[pos] == "."
                    and token
                    and token.group().casefold() in {"tp", "ts", "ths", "pgs", "vd"}
                ):
                    continue
                boundaries.append(pos + 1)
            for end in [*boundaries, pend]:
                raw = text[start:end]
                sentence = raw.strip()
                if sentence:
                    begin = start + len(raw) - len(raw.lstrip())
                    finish = end - (len(raw) - len(raw.rstrip()))
                    if re.match(r"^(?:NEXTGEN\s*\||\d+\s*/\s*\d+\s*$)", sentence):
                        start = end
                        continue
                    if len(sentence) > 3000:
                        raise IndexUnavailable(
                            "Có câu quá dài để giữ nguyên trong child; cần chỉnh cấu trúc nguồn, không cắt âm thầm."
                        )
                    result.append(
                        {
                            "text": sentence,
                            "start": begin,
                            "end": finish,
                            "heading": heading,
                            "block": (left, pstart),
                            "line_start": text.count("\n", 0, begin) + 1,
                            "line_end": text.count("\n", 0, finish) + 1,
                        }
                    )
                start = end
    return result


def complementary(text):
    return bool(
        re.search(
            r"\b(?:phải|chỉ khi|điều kiện|ngoại lệ|trừ khi|lưu ý|tuy nhiên|đồng thời|cần)\b",
            normalized(text),
        )
    )


def make_parents(doc, children, vectors, config):
    groups = []
    for i, child in enumerate(children):
        if groups:
            group = groups[-1]
            previous = children[group[-1]]
            adjacent = child["block"] == previous["block"]
            bounded = child["end"] - children[group[0]]["start"] <= config.get(
                "max_parent_chars", 1800
            )
            related = cosine(vectors[group[-1]], vectors[i]) >= config.get(
                "parent_similarity", 0.48
            ) or complementary(child["text"])
            if adjacent and bounded and related:
                group.append(i)
                continue
        groups.append([i])
    parents = []
    for group in groups:
        first, last = children[group[0]], children[group[-1]]
        text = doc["section_text"][first["start"] : last["end"]]
        pid = doc["id"] + ":p:" + str(first["start"]) + ":" + doc["checksum"][:12]
        parents.append(
            {
                "id": pid,
                "text": text,
                "start": first["start"],
                "end": last["end"],
                "heading": first["heading"],
                "line_start": first["line_start"],
                "line_end": last["line_end"],
                "children": group,
            }
        )
    return parents


def manifest_for(db, doc_id):
    with connect(db) as conn:
        row = conn.execute("SELECT * FROM rag_indexes WHERE document_id=?", (doc_id,)).fetchone()
    if not row:
        return {"status": "not_indexed", "parent_count": 0, "child_count": 0}
    return {
        k: row[k]
        for k in (
            "status",
            "parent_count",
            "child_count",
            "model",
            "model_digest",
            "dimensions",
            "config_hash",
            "source_checksum",
            "error",
            "updated_at",
        )
    }


def index_document(db, doc, encoder=None):
    config = config_for(db)
    if not config or not config.get("enabled"):
        return {"status": "disabled"}
    revision = config_hash(config)
    with connect(db) as conn:
        existing = conn.execute(
            "SELECT * FROM rag_indexes WHERE document_id=?", (doc["id"],)
        ).fetchone()
        if (
            existing
            and existing["status"] == "ready"
            and existing["source_checksum"] == doc["checksum"]
            and existing["config_hash"] == revision
        ):
            return manifest_for(db, doc["id"])
        conn.execute(
            """INSERT INTO rag_indexes(document_id,source_checksum,model,model_digest,dimensions,config_hash,status,updated_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET source_checksum=excluded.source_checksum,
            model=excluded.model,model_digest=excluded.model_digest,dimensions=excluded.dimensions,config_hash=excluded.config_hash,
            status=excluded.status,error=NULL,updated_at=excluded.updated_at""",
            (
                doc["id"],
                doc["checksum"],
                config["model"],
                config["digest"],
                config["dimensions"],
                revision,
                "processing",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    try:
        children = sentences(doc["section_text"])
        if not children:
            raise IndexUnavailable("Không có câu hợp lệ để lập chỉ mục.")
        vectors = (encoder or OllamaEncoder(config)).embed([c["text"] for c in children])
        parents = make_parents(doc, children, vectors, config)
        with connect(db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT checksum FROM documents WHERE id=?", (doc["id"],)
            ).fetchone()
            if (
                not current
                or current[0] != doc["checksum"]
                or config_hash(config_for(db)) != revision
            ):
                raise IndexUnavailable(
                    "Nguồn hoặc cấu hình đổi trong lúc embedding; chỉ mục chưa được kích hoạt."
                )
            conn.execute("DELETE FROM rag_children WHERE document_id=?", (doc["id"],))
            conn.execute("DELETE FROM rag_parents WHERE document_id=?", (doc["id"],))
            for parent in parents:
                conn.execute(
                    "INSERT INTO rag_parents VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        parent["id"],
                        doc["id"],
                        doc["checksum"],
                        parent["start"],
                        parent["end"],
                        parent["text"],
                        parent["heading"],
                        parent["line_start"],
                        parent["line_end"],
                    ),
                )
                for i in parent["children"]:
                    child = children[i]
                    cid = doc["id"] + ":s:" + str(child["start"]) + ":" + doc["checksum"][:12]
                    conn.execute(
                        "INSERT INTO rag_children VALUES(?,?,?,?,?,?,?,?)",
                        (
                            cid,
                            parent["id"],
                            doc["id"],
                            child["start"],
                            child["end"],
                            child["text"],
                            json.dumps(lexical_terms(child["text"]), ensure_ascii=False),
                            array("f", vectors[i]).tobytes(),
                        ),
                    )
            conn.execute(
                "UPDATE rag_indexes SET status='ready',parent_count=?,child_count=?,error=NULL,updated_at=? WHERE document_id=?",
                (len(parents), len(children), datetime.now(timezone.utc).isoformat(), doc["id"]),
            )
        return manifest_for(db, doc["id"])
    except Exception as error:
        message = (
            str(error)
            if isinstance(error, IndexUnavailable)
            else "Lỗi lập chỉ mục; nguồn vẫn được giữ ở staging để xử lý lại."
        )
        with connect(db) as conn:
            conn.execute(
                "UPDATE rag_indexes SET status='failed',error=?,updated_at=? WHERE document_id=?",
                (message, datetime.now(timezone.utc).isoformat(), doc["id"]),
            )
        return manifest_for(db, doc["id"])


def sparse_scores(query, rows):
    terms = set(lexical_terms(query))
    corpus = [json.loads(r["terms_json"]) for r in rows]
    avg = sum(len(t) for t in corpus) / max(1, len(corpus)) or 1
    df = Counter(t for text in corpus for t in set(text))
    scores = {}
    for row, text in zip(rows, corpus):
        tf = Counter(text)
        score = 0.0
        for t in terms:
            if tf[t]:
                idf = math.log(1 + (len(rows) - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * tf[t] * 2.5 / (tf[t] + 1.5 * (0.25 + 0.75 * len(text) / avg))
        if score:
            scores[row["id"]] = score
    return scores


class HybridRetriever:
    def __init__(self, db, encoder=None):
        self.db = db
        self.encoder = encoder
        self.last_trace = None

    @property
    def enabled(self):
        config = config_for(self.db)
        return bool(config and config.get("enabled"))

    def rank_parents(self, question, docs):
        """Caller passes ONLY documents authorized and effective for this request."""
        self.last_trace = None
        if not docs:
            return []
        config = config_for(self.db)
        if not config or not config.get("enabled"):
            raise IndexUnavailable("Hybrid chưa được cấu hình.")
        revision = config_hash(config)
        by_id = {d["id"]: d for d in docs}
        ids = sorted(by_id)
        placeholders = ",".join("?" for _ in ids)
        with connect(self.db) as conn:
            manifests = {
                r["document_id"]: dict(r)
                for r in conn.execute(
                    "SELECT * FROM rag_indexes WHERE document_id IN (" + placeholders + ")", ids
                )
            }
            if any(
                i not in manifests
                or manifests[i]["status"] != "ready"
                or manifests[i]["source_checksum"] != by_id[i]["checksum"]
                or manifests[i]["config_hash"] != revision
                for i in ids
            ):
                raise IndexUnavailable(
                    "Nguồn hợp lệ chưa có đủ chỉ mục embedding đúng revision; hãy xử lý lại chỉ mục trước khi hỏi."
                )
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM rag_children WHERE document_id IN ("
                    + placeholders
                    + ") ORDER BY id",
                    ids,
                )
            ]
            parents = {
                r["id"]: dict(r)
                for r in conn.execute(
                    "SELECT * FROM rag_parents WHERE document_id IN (" + placeholders + ")", ids
                )
            }
        from .query_normalization import normalize_query

        original_question = question
        question, query_changes = normalize_query(
            question, {term for row in rows for term in json.loads(row["terms_json"])}
        )
        query_vector = (self.encoder or OllamaEncoder(config)).embed([question], query=True)[0]
        dense = {r["id"]: cosine(query_vector, array("f", r["embedding"])) for r in rows}
        sparse = sparse_scores(question, rows)
        dense_order = sorted(dense, key=lambda k: (-dense[k], k))[:40]
        sparse_order = sorted(sparse, key=lambda k: (-sparse[k], k))[:40]
        fused = {}
        for ranking in (dense_order, sparse_order):
            for rank, cid in enumerate(ranking, 1):
                fused[cid] = fused.get(cid, 0) + 1 / (config.get("rrf_k", 60) + rank)
        child_by_id = {r["id"]: r for r in rows}
        parent_results = {}
        for cid, score in sorted(fused.items(), key=lambda x: (-x[1], x[0])):
            row = child_by_id[cid]
            parent = parents[row["parent_id"]]
            if cid not in parent_results.get(parent["id"], {}).get("child_ids", []):
                entry = parent_results.setdefault(
                    parent["id"],
                    {
                        "parent": parent,
                        "rrf": score,
                        "dense": dense[cid],
                        "sparse": sparse.get(cid, 0),
                        "child_ids": [],
                    },
                )
                entry["rrf"] = max(entry["rrf"], score)
                entry["dense"] = max(entry["dense"], dense[cid])
                entry["sparse"] = max(entry["sparse"], sparse.get(cid, 0))
                entry["child_ids"].append(cid)
        trace = {
            "method": "dense+bm25+rrf",
            "model": config["model"],
            "model_digest": config["digest"],
            "dimensions": config["dimensions"],
            "config_hash": revision,
            "splitter_revision": REVISION,
            "filtered_document_ids": ids,
            "dense_child_ids": dense_order,
            "bm25_child_ids": sparse_order,
            "parents": [],
        }
        if query_changes:
            trace["query_normalization"] = {
                "original": original_question,
                "normalized": question,
                "changes": query_changes,
            }
        output = []
        for entry in sorted(
            parent_results.values(), key=lambda e: (-e["rrf"], -e["dense"], e["parent"]["id"])
        ):
            p = entry["parent"]
            doc = by_id[p["document_id"]]
            if (
                p["source_checksum"] != doc["checksum"]
                or doc["section_text"][p["start_offset"] : p["end_offset"]] != p["text"]
            ):
                raise IndexUnavailable("Parent không khớp nội dung gốc; không phát đoạn trích.")
            part = {
                "id": p["id"],
                "title": p["heading"],
                "section_text": p["text"],
                "doc": doc,
                "offset": p["start_offset"],
                "line_start": p["line_start"],
                "line_end": p["line_end"],
                "dense_score": entry["dense"],
                "sparse_score": entry["sparse"],
                "child_ids": entry["child_ids"],
            }
            output.append((part, entry["rrf"]))
            trace["parents"].append(
                {
                    "parent_id": p["id"],
                    "document_id": doc["id"],
                    "child_ids": entry["child_ids"],
                    "dense_score": round(entry["dense"], 6),
                    "rrf_score": round(entry["rrf"], 6),
                }
            )
        self.last_trace = trace
        return output

    def rank_documents(self, question, docs):
        ranked = self.rank_parents(question, docs)
        by_id = {}
        for part, score in ranked:
            by_id.setdefault(part["doc"]["id"], (part["doc"], score))
        return list(by_id.values())

    def verify(self, trace):
        config = config_for(self.db)
        if not config or trace["config_hash"] != config_hash(config):
            return False
        with connect(self.db) as conn:
            for doc_id in trace["filtered_document_ids"]:
                row = conn.execute(
                    "SELECT status,config_hash,source_checksum FROM rag_indexes WHERE document_id=?",
                    (doc_id,),
                ).fetchone()
                doc = conn.execute(
                    "SELECT checksum FROM documents WHERE id=?", (doc_id,)
                ).fetchone()
                if (
                    not row
                    or not doc
                    or row["status"] != "ready"
                    or row["config_hash"] != trace["config_hash"]
                    or row["source_checksum"] != doc[0]
                ):
                    return False
        return True
