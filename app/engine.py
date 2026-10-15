"""Evidence-first answering: ACL and rule selection stay outside generation."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
import uuid
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from .store import (
    DEFAULT_DB,
    ROOT,
    access_tree,
    all_capabilities,
    applicable,
    binding_subjects,
    bindings_for,
    can_action,
    can_create_group,
    can_read,
    connect,
    document_records,
    public_user,
    user_record,
    roles_for,
    query_candidates,
    today_local,
    validity_info,
    validate_date,
    within_company_boundary,
)
from .conversation import leave_dialogue
from .leave_answers import answer_leave
from .document_answers import answer_documents
from .policy_scope import (
    resolve_scope,
    scope_documents,
    lookup_context,
    short_scope_reply,
    money_review,
)
from .hybrid import HybridRetriever, IndexUnavailable, config_for
from .rag_answers import answer_rag, missing as rag_missing
from .generation import generation_config
from .document_lookup import lookup_documents

MODE = "extractive"
STATUS_LABELS = {
    "answered": "Có căn cứ",
    "needs_context": "Cần thêm bối cảnh",
    "insufficient_evidence": "Chưa đủ căn cứ",
    "conflict": "Nguồn mâu thuẫn",
    "unsupported": "Chưa hỗ trợ",
    "error": "Lỗi xử lý",
}
TOPICS = {
    "meal": ("an toi", "tien an", "bua toi", "suat an", "ho tro an", "lam them", "tang ca"),
    "remote": ("lam viec tu xa", "work from home", "wfh", "remote"),
    "bonus": ("thuong", "bonus"),
}
STOP = set(
    "tôi bạn được của cho là có và nếu thì một các bao nhiêu nào khi theo với những này đó trong từ đến sau hỏi hãy giúp thế sao giờ công ty nhân viên hỗ trợ quy định".split()
)


def normalized(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def ascii_text(text: str) -> str:
    value = unicodedata.normalize("NFD", text.casefold().replace("đ", "d"))
    return "".join(c for c in value if unicodedata.category(c) != "Mn")


def tokens(text: str) -> list[str]:
    # Use the same accent folding for query and documents, including stop words.
    stop = {ascii_text(word) for word in STOP}
    return [
        word
        for word in re.findall(r"[^\W_]+", ascii_text(text))
        if word not in stop and len(word) > 1
    ]


def unavailable_policy(question: str) -> str | None:
    text = ascii_text(question)
    for label, phrases in (
        ("nghỉ phép năm", ("nghi phep", "phep nam")),
        ("nghỉ lễ", ("nghi le", "ngay le", "nghi tet")),
        ("nghỉ ốm", ("nghi om", "om dau")),
        ("thai sản", ("thai san",)),
        ("bảo hiểm", ("bao hiem",)),
    ):
        if any(re.search(r"\b" + re.escape(phrase) + r"\b", text) for phrase in phrases):
            return label
    return None


def clarification(question: str) -> tuple[str, list[dict], str]:
    return (
        "Tôi chưa xác định rõ chính sách bạn muốn hỏi. Bạn đang hỏi về hỗ trợ tiền ăn, làm việc từ xa, thưởng hay chế độ nào khác? Hãy nói rõ chính sách và điều bạn cần biết để tôi tra cứu đúng phạm vi.",
        [
            {"label": "Hỗ trợ tiền ăn", "question": "Quy định hỗ trợ tiền ăn của tôi là gì?"},
            {"label": "Làm việc từ xa", "question": "Tôi được làm việc từ xa mấy ngày mỗi tuần?"},
        ],
        "policy_topic",
    )


def topic_for(question: str) -> str | None:
    text = ascii_text(question)
    # Explicitly unknown policies must not be conflated with generic employment words.
    if unavailable_policy(question):
        return None
    scored = {key: sum(phrase in text for phrase in phrases) for key, phrases in TOPICS.items()}
    highest = max(scored.values())
    winners = [key for key, score in scored.items() if score == highest]
    return winners[0] if highest and len(winners) == 1 else None


def bm25(question: str, docs: list[dict]) -> list[tuple[dict, float]]:
    if not docs:
        return []
    corpus = [tokens(doc["title"] + " " + doc["section_text"]) for doc in docs]
    avg_length = sum(map(len, corpus)) / len(corpus) or 1
    query = set(tokens(question))
    document_frequency = Counter(term for text in corpus for term in set(text))
    results = []
    for doc, text in zip(docs, corpus):
        frequency = Counter(text)
        score = 0.0
        for term in query:
            tf = frequency[term]
            if tf:
                inverse = math.log(
                    1
                    + (len(corpus) - document_frequency[term] + 0.5)
                    / (document_frequency[term] + 0.5)
                )
                score += inverse * (tf * 2.5) / (tf + 1.5 * (0.25 + 0.75 * len(text) / avg_length))
        if score > 0:
            results.append((doc, round(score, 5)))
    return sorted(results, key=lambda item: (-item[1], item[0]["id"]))


def number_from(doc: dict) -> tuple[int, str, str] | None:
    pattern = r"(?P<amount>\d+(?:[.,]\d{3})*)\s*đồng/(?P<unit>ca|kỳ)"
    matches = list(re.finditer(pattern, doc["section_text"]))
    if not matches:
        matches = list(re.finditer(r"(?P<amount>\d+)\s*(?P<unit>ngày/tuần)", doc["section_text"]))
    if not matches:
        return None
    if len({(re.sub(r"[.,]", "", m.group("amount")), m.group("unit")) for m in matches}) != 1:
        return None
    match = matches[0]
    value = int(re.sub(r"[.,]", "", match.group("amount")))
    # Quote the full original sentence containing the value, including its conditions.
    start = doc["section_text"].rfind("\n", 0, match.start()) + 1
    end = doc["section_text"].find("\n", match.end())
    quote = doc["section_text"][start : end if end >= 0 else None].strip()
    return value, match.group("unit"), quote


def citation(doc: dict, quote: str | None = None) -> dict:
    quote = quote or doc["section_text"].split("\n", 1)[0]
    if quote not in doc["section_text"]:
        raise ValueError("Citation quote not found in source")
    output = {
        "id": doc["id"],
        "title": doc["title"],
        "version": doc["version"],
        "section": doc["section"],
        "quote": quote,
        "effective_from": doc["effective_from"],
        "effective_to": doc["effective_to"],
        "checksum": doc["checksum"],
    }
    if "origin" in doc:
        output["origin"] = {
            key: doc["origin"].get(key)
            for key in (
                "filename",
                "media_type",
                "file_checksum",
                "extractor",
                "page_count",
                "note",
            )
        }
    return output


def fingerprint(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


class KnowledgeEngine:
    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = Path(db_path)

    def bootstrap(self) -> dict:
        with connect(self.db_path) as conn:
            users = [
                public_user(user_record(conn, row[0]))
                for row in conn.execute("SELECT id FROM users ORDER BY id")
            ]
            bindings = bindings_for(conn)
            docs = document_records(conn)
            for output_user in users:
                actual_user = user_record(conn, output_user["id"])
                output_user["create_group"] = can_create_group(conn, actual_user, bindings)
                output_user["permissions"] = all_capabilities(actual_user, bindings, docs)
                output_user["create_companies"] = [
                    row["id"]
                    for row in conn.execute(
                        "SELECT id FROM organizations WHERE level='company' ORDER BY id"
                    )
                    if can_action(
                        actual_user,
                        "document.create_version",
                        bindings,
                        {
                            "applies_company": row["id"],
                            "status": "staging",
                            "owner_id": actual_user["id"],
                            "allow": [],
                            "deny": [],
                        },
                    )
                ]
                scopes = []
                for org in conn.execute(
                    "SELECT id,level,parent_id FROM organizations WHERE level IN ('company','department') ORDER BY id"
                ):
                    company = org["id"] if org["level"] == "company" else org["parent_id"]
                    department = org["id"] if org["level"] == "department" else None
                    if can_action(
                        actual_user,
                        "document.create_version",
                        bindings,
                        {
                            "applies_company": company,
                            "applies_department": department,
                            "status": "staging",
                            "owner_id": actual_user["id"],
                            "allow": [],
                            "deny": [],
                        },
                    ):
                        scopes.append({"company_id": company, "department_id": department})
                output_user["create_scopes"] = scopes
                output_user["create_companies"] = sorted({s["company_id"] for s in scopes})
            orgs = [
                dict(row) for row in conn.execute("SELECT * FROM organizations ORDER BY level,id")
            ]
            revision = conn.execute(
                "SELECT value FROM settings WHERE key='dataset_revision'"
            ).fetchone()[0]
            reader_tree = access_tree(conn)
            role_labels = roles_for(conn)
            for output_user in users:
                role_labels.setdefault(output_user["role"], output_user["role"])
            roles = [{"id": key, "name": value} for key, value in roles_for(conn).items()]
        rag = config_for(self.db_path)
        generator = generation_config(self.db_path)
        return {
            "users": users,
            "organizations": orgs,
            "mode": "hybrid_rag" if rag and rag.get("enabled") else MODE,
            "models": {
                "embedding": rag.get("model") if rag else None,
                "generation": generator.get("model") if generator else None,
                "generation_provider": generator.get("provider", "ollama") if generator else None,
            },
            "dataset_revision": revision,
            "default_as_of": today_local(),
            "status_labels": STATUS_LABELS,
            "role_labels": role_labels,
            "demo_identity": True,
            "access_tree": reader_tree,
            "roles": roles,
        }

    def source(
        self, user_id: str, source_id: str, request_id=None, citation_index=None, passage_only=False
    ) -> dict | None:
        if passage_only and request_id is None:
            return None
        with connect(self.db_path) as conn:
            user = user_record(conn, user_id)
            doc = next((item for item in document_records(conn) if item["id"] == source_id), None)
            if not user or not doc or not can_read(user, doc):
                return None
            result = citation(doc)
            result["text"] = doc["source_text"]
            if request_id is not None:
                row = conn.execute(
                    "SELECT response_json FROM query_runs WHERE request_id=? AND user_id=?",
                    (request_id, user_id),
                ).fetchone()
                if not row or not isinstance(citation_index, int) or citation_index < 1:
                    return None
                archived = json.loads(row[0])
                citations = archived.get("citations", [])
                if citation_index > len(citations):
                    return None
                cited = citations[citation_index - 1]
                if cited["id"] != source_id or cited["checksum"] != doc["checksum"]:
                    return None
                begin = cited.get("start_offset")
                end = cited.get("end_offset")
                if (
                    isinstance(begin, int)
                    and isinstance(end, int)
                    and 0 <= begin < end <= len(doc["section_text"])
                ):
                    if doc["section_text"][begin:end] != cited["quote"]:
                        return None
                    marker = "## " + doc["section"] + "\n"
                    marker_start = doc["source_text"].find(marker)
                    if marker_start < 0:
                        return None
                    after = marker_start + len(marker)
                    section_start = (
                        after
                        + len(doc["source_text"][after:])
                        - len(doc["source_text"][after:].lstrip())
                    )
                    if (
                        doc["source_text"][section_start : section_start + len(doc["section_text"])]
                        != doc["section_text"]
                    ):
                        return None
                    begin += section_start
                    end += section_start
                else:
                    begin = doc["source_text"].find(cited["quote"])
                    end = begin + len(cited["quote"])
                    if begin < 0:
                        return None
                if passage_only:
                    # Show the exact cited span with bounded surrounding context.
                    # The full file remains available only through an explicit open/download.
                    left = max(0, begin - 240)
                    right = min(len(result["text"]), end + 240)
                    line_left = result["text"].find("\n", left, begin)
                    line_right = result["text"].rfind("\n", end, right)
                    if line_left >= 0:
                        left = line_left + 1
                    if line_right >= 0:
                        right = line_right
                    result["view"] = "passage"
                    result["citation_location"] = cited.get("location", cited["section"])
                    result["line_start"] = cited.get("line_start")
                    result["line_end"] = cited.get("line_end")
                    result["text"] = result["text"][left:right]
                    begin -= left
                    end -= left
                # Browser string offsets use UTF-16, while stored source offsets use Unicode code points.
                js_begin = len(result["text"][:begin].encode("utf-16-le")) // 2
                js_end = len(result["text"][:end].encode("utf-16-le")) // 2
                result["highlights"] = [
                    {
                        "start": js_begin,
                        "end": js_end,
                        "quote": cited["quote"],
                        "citation_index": citation_index,
                    }
                ]
                result["highlight_request_id"] = request_id
            return result

    def library(self, user_id: str, as_of=None) -> dict:
        as_of = as_of or today_local()
        validate_date(as_of)
        with connect(self.db_path) as conn:
            user = user_record(conn, user_id)
            if not user:
                raise PermissionError("Bối cảnh không hợp lệ.")
            docs = document_records(conn)
            relations = [dict(r) for r in conn.execute("SELECT * FROM relations WHERE approved=1")]
            return {
                "as_of": as_of,
                "documents": [
                    {**citation(doc), **validity_info(doc, as_of, user, docs, relations)}
                    for doc in docs
                    if can_read(user, doc)
                ],
            }

    def answer(self, payload: dict, before_emit: Callable | None = None) -> dict:
        start = time.perf_counter()
        if not isinstance(payload, dict):
            raise ValueError("Payload phải là object JSON.")
        user_id, question, as_of = (
            payload.get("user_id"),
            payload.get("question"),
            payload.get("as_of", today_local()),
        )
        if (
            not isinstance(user_id, str)
            or not isinstance(question, str)
            or not question.strip()
            or len(question) > 4000
        ):
            raise ValueError("Cần user_id và câu hỏi từ 1 đến 4.000 ký tự.")
        if not isinstance(as_of, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
            raise ValueError("Ngày áp dụng phải theo định dạng YYYY-MM-DD.")
        try:
            date.fromisoformat(as_of)
        except ValueError:
            raise ValueError("Ngày áp dụng không hợp lệ.") from None
        question = question.strip()
        from .query_normalization import normalize_query

        original_question = question
        question, query_changes = normalize_query(question)
        retriever = HybridRetriever(self.db_path)
        parent_id = payload.get("parent_request_id")
        if parent_id is not None and (
            not isinstance(parent_id, str) or not re.fullmatch(r"[0-9a-f]{32}", parent_id)
        ):
            raise ValueError("Tham chiếu hội thoại không hợp lệ.")
        previous = None
        with connect(self.db_path) as conn:
            user = user_record(conn, user_id)
            if not user:
                raise ValueError("Hồ sơ demo không tồn tại.")
            docs = document_records(conn)
            relations = [
                dict(row) for row in conn.execute("SELECT * FROM relations WHERE approved=1")
            ]
            bindings = bindings_for(conn)
            if not can_action(user, "query.ask", bindings):
                raise PermissionError("Bối cảnh hiện tại không có quyền hỏi đáp.")
            organizations = [dict(row) for row in conn.execute("SELECT * FROM organizations")]
            if parent_id:
                parent = conn.execute(
                    "SELECT user_id,response_json FROM query_runs WHERE request_id=?", (parent_id,)
                ).fetchone()
                if not parent or parent["user_id"] != user_id:
                    raise PermissionError("Không thể sử dụng tham chiếu hội thoại này.")
                archived = json.loads(parent["response_json"])
                previous = archived.get("conversation_state")
                # Existing conversations predate policy memory; recover only the archived intent/scope.
                if (
                    not previous
                    and archived.get("trace", {}).get("policy_key") in TOPICS
                    and archived.get("policy_scope")
                ):
                    previous = {
                        "topic": archived["trace"]["policy_key"],
                        "pending_scope": False,
                        "question": archived["question"],
                        "policy_scope": archived["policy_scope"],
                    }
        result = {
            "request_id": uuid.uuid4().hex,
            "status": "insufficient_evidence",
            "mode": MODE,
            "user": public_user(user),
            "question": original_question,
            "as_of": as_of,
            "answer": "Kho hiện tại chưa có đủ nguồn được phép sử dụng để trả lời câu hỏi này.",
            "_snapshot": {
                "mode": MODE,
                "model": None,
                "user_context": user,
                "as_of": as_of,
                "question": original_question,
                "query_normalization": {"normalized": question, "changes": query_changes},
                "sources": [],
                "policy_relations": [],
                "parent_request_id": parent_id,
                "bindings": [b for b in bindings if b["subject"] in binding_subjects(user)],
                "instructions": "Chỉ trích từ nguồn được phép và áp dụng; không suy quyền/hiệu lực; thiếu nguồn hoặc xung đột thì chưa kết luận.",
                "config": {"retrieval": "BM25", "k1": 1.5, "b": 0.75, "top_k": 5, "topics": TOPICS},
            },
            "citations": [],
            "amount": None,
            "unit": None,
            "trace": {
                "policy_key": None,
                "retrieved_ids": [],
                "selected_ids": [],
                "retrieval": [],
                "sources_sent_to_llm": [],
                "relations_used": [],
            },
            "steps": [
                {
                    "label": "Bối cảnh",
                    "state": "done",
                    "detail": "Lấy hồ sơ giả lập từ dữ liệu server.",
                },
                {"label": "Quyền & hiệu lực", "state": "pending", "detail": "Chưa thực hiện."},
                {"label": "Tìm & kiểm nguồn", "state": "pending", "detail": "Chưa thực hiện."},
                {"label": "Trả lời & citation", "state": "pending", "detail": "Chưa thực hiện."},
            ],
        }
        if retriever.enabled:
            result["_snapshot"]["config"] = {
                "retrieval": "dense+bm25+rrf",
                "embedding": config_for(self.db_path),
                "topics": TOPICS,
            }

        def general_answer(explicit=False):
            if not retriever.enabled:
                return answer_documents(
                    user,
                    docs,
                    relations,
                    question,
                    as_of,
                    tokens,
                    bm25,
                    citation,
                    explicit_only=explicit,
                )
            try:
                output = answer_rag(
                    self.db_path,
                    user,
                    docs,
                    relations,
                    question,
                    as_of,
                    tokens,
                    citation,
                    retriever,
                    explicit_only=explicit,
                    authorize=lambda: self._authorize_generation(user, docs, relations),
                )
                if output is not None:
                    return output
                lookup = answer_documents(
                    user,
                    docs,
                    relations,
                    question,
                    as_of,
                    tokens,
                    bm25,
                    citation,
                    explicit_only=True,
                )
                if lookup and lookup.get("answer_kind") == "document_lookup":
                    return lookup
                return None
            except IndexUnavailable as error:
                failed = rag_missing(str(error), retriever)
                if getattr(error, "context", None):
                    failed["_generation"] = error.context
                    ids = set(error.context.get("source_ids", []))
                    failed["_document_sources"] = [d for d in docs if d["id"] in ids]
                    failed["trace"]["retrieved_ids"] = sorted(ids)
                    failed["trace"]["sources_sent_to_llm"] = sorted(ids)
                return failed

        if not user["company_id"] or (
            not user["department_id"]
            and user.get("organization_level") != "group"
            and user["company_id"]
            not in user.get("organization_read_scope", {}).get("allowed_ids", [])
        ):
            result.update(
                status="needs_context",
                answer="Bạn thuộc công ty và phòng ban nào? Hồ sơ hiện chưa có đơn vị được xác nhận. Trong bản demo, hãy chọn hồ sơ có đủ đơn vị; câu trả lời trong chat không tự cấp quyền truy cập.",
                clarification_kind="identity_context",
            )
            result["steps"][0].update(
                state="warning", detail="Thiếu đơn vị/phòng ban được xác nhận."
            )
            return self._finish(result, start)
        try:
            found = lookup_documents(user, docs, relations, question, as_of, citation, retriever)
        except IndexUnavailable as error:
            found = rag_missing(str(error), retriever)
        if found is not None:
            from .document_lookup import lookup_words
            from .document_answers import title_key

            title_terms = set().union(
                *(lookup_words(d["title"]) for d in found.get("_document_sources", []))
            )
            descriptive = lookup_words(question) - title_terms
            if (
                retriever.enabled
                and found.get("status") == "answered"
                and descriptive
                and re.search(r"\b(workflow|quy trinh|luong)\b", title_key(question))
                and not re.match(r"^(mo|tai lieu|file|tim (tai lieu|file))\b", title_key(question))
            ):
                overview = general_answer(True)
                if overview is not None:
                    found = overview
            return self._finish(
                self._document_result(result, found, user, docs, relations, before_emit), start
            )
        # An explicitly named general document is a lookup request, even if it mentions policy words.
        general = general_answer(True)
        if general:
            return self._finish(
                self._document_result(result, general, user, docs, relations, before_emit), start
            )
        dialogue = leave_dialogue(question, as_of, previous)
        if dialogue:
            result.update(dialogue)
            result["_snapshot"]["conversation_state"] = dialogue["conversation_state"]
            result["steps"][2].update(
                state="warning",
                detail="Phân biệt quyền nghỉ, số ngày đã nghỉ và số dư; chỉ hỏi thông tin còn thiếu, không dùng hội thoại làm dữ liệu quyền lợi.",
            )
            supported = answer_leave(user, docs, relations, dialogue, as_of, citation)
            if supported:
                sources = supported.pop("_leave_sources", [])
                used = supported.pop("_leave_relations", [])
                result.update(supported)
                result["_snapshot"]["sources"] = [
                    {
                        key: doc[key]
                        for key in (
                            "id",
                            "title",
                            "version",
                            "section",
                            "section_text",
                            "checksum",
                            "effective_from",
                            "effective_to",
                            "facts",
                        )
                    }
                    for doc in sources
                ]
                result["_snapshot"]["policy_relations"] = used
                result["steps"][1].update(
                    state="done",
                    detail="Kiểm quyền đọc, phạm vi, chủ hồ sơ và kỳ dữ liệu nguồn import.",
                )
                result["steps"][2].update(
                    state="done" if result["status"] == "answered" else "warning",
                    detail="Trích số ngày từ nguồn đã duyệt/công bố; không tính từ lời người dùng.",
                )
                result = self._recheck(result, user, docs, relations, before_emit)
            return self._finish(result, start)
        dates = set(re.findall(r"\b\d{1,2}/\d{1,2}/\d{4}\b|\b\d{4}-\d{2}-\d{2}\b", question))
        if len(dates) > 1:
            result.update(
                status="unsupported",
                answer="MVP hiện chỉ xử lý một thời điểm áp dụng mỗi lần hỏi. Chưa hỗ trợ tổng hợp nhiều giai đoạn trong cùng câu hỏi; hãy tách câu hỏi theo từng thời điểm.",
            )
            return self._finish(result, start)
        if dates:
            mentioned = next(iter(dates))
            try:
                if "/" in mentioned:
                    day, month, year = map(int, mentioned.split("/"))
                    mentioned_date = date(year, month, day).isoformat()
                else:
                    mentioned_date = date.fromisoformat(mentioned).isoformat()
            except ValueError:
                result.update(
                    status="needs_context",
                    answer="Ngày được nhắc trong câu hỏi chưa hợp lệ. Vui lòng xác nhận thời điểm cần tra cứu.",
                )
                return self._finish(result, start)
            if mentioned_date != as_of:
                result.update(
                    status="needs_context",
                    answer="Ngày trong câu hỏi khác thời điểm đang chọn. Vui lòng xác nhận bằng cách chọn lại ngày áp dụng trước khi tra cứu.",
                )
                return self._finish(result, start)
        topic = topic_for(question)
        review_amount = (
            money_review(question, ascii_text)
            if previous
            and previous.get("topic") in ("meal", "bonus")
            and previous.get("policy_scope")
            else None
        )
        scope_reply = bool(
            previous
            and previous.get("pending_scope")
            and topic is None
            and short_scope_reply(question, organizations, ascii_text)
        )
        if scope_reply:
            topic = previous["topic"]
        if review_amount is not None:
            topic = previous["topic"]
            result["follow_up_context"] = {
                "kind": "amount_review",
                "parent_request_id": parent_id,
                "claimed_amount": review_amount,
                "original_question": previous["question"],
            }
            result["_snapshot"]["follow_up_context"] = result["follow_up_context"]
        if topic is None and retriever.enabled and not unavailable_policy(question):
            try:
                policy_candidates = [
                    d
                    for d in query_candidates(user, docs, relations, as_of)
                    if d["policy_key"] in TOPICS and can_read(user, d)
                ]
                semantic = retriever.rank_parents(question, policy_candidates)
                scores = {}
                for part, _ in semantic:
                    key = part["doc"]["policy_key"]
                    scores[key] = max(scores.get(key, -1), part["dense_score"])
                order = sorted(scores, key=scores.get, reverse=True)
                if (
                    order
                    and scores[order[0]] >= 0.64
                    and (len(order) < 2 or scores[order[0]] - scores[order[1]] >= 0.08)
                ):
                    topic = order[0]
            except IndexUnavailable:
                pass  # A subsequent content lookup reports unavailable instead of inventing an answer.
        result["trace"]["policy_key"] = topic
        if topic is None:
            general = general_answer()
            if general:
                return self._finish(
                    self._document_result(result, general, user, docs, relations, before_emit),
                    start,
                )
            missing_policy = unavailable_policy(question)
            if missing_policy:
                result.update(
                    answer=f"Bạn đang hỏi về {missing_policy}. Bản demo hiện chưa hỗ trợ tra cứu chính sách này và chưa có nguồn được phép dùng để xác định quyền lợi. Tôi chưa thể trả lời mức hoặc số ngày; cần bổ sung tài liệu được duyệt và công bố trong phạm vi của bạn.",
                    reason="policy_not_available",
                )
                result["steps"][2].update(
                    state="warning",
                    detail="Chủ đề đã rõ nhưng nằm ngoài các chính sách được hỗ trợ trong bản demo.",
                )
            else:
                if retriever.enabled and len(tokens(question)) >= 2:
                    result.update(
                        answer="Chưa tìm thấy đủ căn cứ trả lời câu hỏi này trong các nguồn được phép dùng và còn hiệu lực. Tôi chưa kết luận; cần bổ sung nguồn hoặc bối cảnh cụ thể hơn.",
                        reason="no_permitted_evidence",
                    )
                    return self._finish(result, start)
                prompt, options, kind = clarification(question)
                result.update(
                    status="needs_context",
                    answer=prompt,
                    follow_up_options=options,
                    clarification_kind=kind,
                    reason="question_needs_clarification",
                )
                result["steps"][2].update(
                    state="warning",
                    detail="Cần làm rõ câu hỏi trước khi tra cứu; chưa kết luận rằng kho thiếu nguồn.",
                )
            return self._finish(result, start)
        scope = resolve_scope(question, user, organizations, ascii_text)
        if review_amount is not None:
            scope = (
                resolve_scope("Theo hồ sơ của tôi", user, organizations, ascii_text)
                if previous["policy_scope"]["level"] == "personal"
                else previous["policy_scope"]
            )
        frame = {
            "topic": topic,
            "pending_scope": scope is None,
            "question": (
                previous["question"] if scope_reply or review_amount is not None else question
            ),
            "policy_scope": scope,
        }
        result["conversation_state"] = frame
        result["_snapshot"]["conversation_state"] = frame
        if scope is None:
            result["conversation_state"] = frame
            result["_snapshot"]["conversation_state"] = frame
            result.update(
                status="needs_context",
                clarification_kind="policy_scope",
                reason="question_needs_clarification",
                answer="Bạn muốn hỏi quy định cấp tập đoàn, chính sách chung của công ty nào, hay quyền lợi áp dụng cho chính bạn? Tôi cần rõ phạm vi trước khi chọn tài liệu.",
                follow_up_options=[
                    {"label": "Tập đoàn", "question": "Tập đoàn"},
                    *(
                        [{"label": user["company_name"], "question": user["company_name"]}]
                        if user.get("organization_level") != "group"
                        else [
                            {"label": o["name"], "question": o["name"]}
                            for o in organizations
                            if o["level"] == "company"
                            and o["id"]
                            in user.get("organization_read_scope", {}).get("allowed_ids", [])
                        ]
                    ),
                    {"label": "Theo hồ sơ của tôi", "question": "Theo hồ sơ của tôi"},
                ],
            )
            result["steps"][2].update(
                state="warning",
                detail="Chưa xác định phạm vi được hỏi; chưa truy xuất hoặc chọn mức tiền.",
            )
            return self._finish(result, start)
        result["policy_scope"] = scope
        result["_snapshot"]["policy_scope"] = scope
        if scope["company_id"] and not within_company_boundary(
            user, {"applies_company": scope["company_id"]}
        ):
            result.update(
                reason="company_scope_denied",
                answer="Hồ sơ của bạn không thuộc công ty được hỏi. Tài liệu nội bộ công ty chỉ được đọc bởi nhân sự của chính công ty đó có ACL phù hợp; tôi không sử dụng hoặc trả thay bằng chính sách của công ty khác.",
            )
            return self._finish(result, start)
        lookup_docs = scope_documents(scope, docs)
        eligible = [
            doc
            for doc in query_candidates(lookup_context(scope, user), lookup_docs, relations, as_of)
            if doc["policy_key"] == topic
        ]
        readable = [doc for doc in eligible if can_read(user, doc)]
        result["steps"][1].update(
            state="done", detail="Lọc quyền hiện hành, đối tượng và thời điểm áp dụng."
        )
        # Metadata declares required rules; never use a fallback rule when a mandatory exception is unreadable.
        if any(doc["required"] and not can_read(user, doc) for doc in eligible):
            result["steps"][2].update(
                state="warning", detail="Chưa xác nhận đủ căn cứ để kết luận."
            )
            return self._finish(result, start)
        try:
            ranked = (
                retriever.rank_documents(frame["question"], readable)
                if retriever.enabled
                else bm25(frame["question"], readable)
            )
        except IndexUnavailable as error:
            result.update(reason="index_unavailable", answer=str(error))
            return self._finish(result, start)
        if retriever.last_trace:
            result["trace"]["hybrid"] = retriever.last_trace
            result["mode"] = "hybrid_extract"
        result["_snapshot"]["sources"] = [
            {
                key: doc[key]
                for key in (
                    "id",
                    "title",
                    "version",
                    "section",
                    "section_text",
                    "checksum",
                    "effective_from",
                    "effective_to",
                )
            }
            for doc in readable
        ]
        result["trace"]["retrieval"] = [
            {"id": doc["id"], "score": score} for doc, score in ranked[:5]
        ]
        result["trace"]["retrieved_ids"] = [doc["id"] for doc, _ in ranked[:5]]
        if not ranked:
            result["steps"][2].update(
                state="warning", detail="Không tìm thấy đoạn phù hợp trong phạm vi hợp lệ."
            )
            return self._finish(result, start)
        primary = [doc for doc in readable if doc["required"]]
        active_ids = {doc["id"] for doc in primary}
        used = [
            r for r in relations if r["source_id"] in active_ids and r["target_id"] in active_ids
        ]
        replaced = {r["target_id"] for r in used}
        winners = [doc for doc in primary if doc["id"] not in replaced]
        result["trace"]["relations_used"] = used
        result["_snapshot"]["policy_relations"] = used
        if not winners:
            result["steps"][2].update(
                state="warning", detail="Chưa có quy định đủ căn cứ để áp dụng."
            )
            return self._finish(result, start)
        result["trace"]["selected_ids"] = [doc["id"] for doc in winners]
        if len(winners) > 1:
            result.update(
                status="conflict",
                answer="Có các nguồn cùng hiệu lực và cùng phạm vi nhưng nội dung chưa được giải quyết bằng quan hệ ưu tiên đã duyệt. Chưa thể kết luận; cần người có thẩm quyền xác minh.",
                citations=[citation(doc) for doc in winners],
            )
            result["steps"][2].update(
                state="warning", detail="Nguồn cùng áp dụng chưa có ưu tiên được xác nhận."
            )
        else:
            winner = winners[0]
            extracted = number_from(winner)
            if not extracted:
                result["steps"][2].update(
                    state="warning", detail="Chưa trích được mức hỗ trợ từ nguồn gốc."
                )
                return self._finish(result, start)
            amount, unit, quote = extracted
            if topic == "meal" and unit != "ca":
                result["steps"][2].update(
                    state="warning", detail="Nguồn chưa có mức tiền ăn theo đơn vị đồng/ca hợp lệ."
                )
                return self._finish(result, start)
            # Identify the actual source, preserving its original conditions.
            if topic == "meal":
                formatted = f"{amount:,}".replace(",", ".")
                answer = f"Mức hỗ trợ theo chính sách: {formatted} đồng/{unit}.\n\nQuy định và điều kiện theo tài liệu \u201c{winner['title']}\u201d ({winner['section']}):\n{quote}\n\nĐây là mức theo chính sách; chưa xác nhận bạn có ca đáp ứng các điều kiện trên."
            else:
                answer = (
                    f"Theo tài liệu \u201c{winner['title']}\u201d ({winner['section']}): {quote}"
                )
            answer = answer.replace(f"{amount:,}", f"{amount:,}".replace(",", "."))
            if review_amount is not None:
                claimed = f"{review_amount:,}".replace(",", ".")
                comparison = (
                    "Mức bạn nhắc khớp với số tiền trong nguồn đã kiểm lại."
                    if review_amount == amount
                    else f"Chưa có căn cứ xác nhận mức {claimed} đồng cho phạm vi và ngày đang tra cứu."
                )
                answer = (
                    f'Bạn đang đối chiếu mức {claimed} đồng. Tôi đã tra cứu lại nguồn theo ngày {date.fromisoformat(as_of).strftime("%d/%m/%Y")}. {comparison}\n\n'
                    + answer
                )
            answer = f"Phạm vi đang tra cứu: {scope['label']}.\n\n" + answer
            result.update(
                status="answered",
                answer=answer,
                amount=amount,
                unit=unit,
                citations=[citation(winner, quote)],
            )
            result["steps"][2].update(
                state="done", detail="Áp dụng quan hệ chính sách đã duyệt; giữ điều kiện gốc."
            )
            result["steps"][3].update(
                state="done", detail="Trích xuất từ nguồn; kiểm ID, phiên bản, đoạn trích và quyền."
            )
        result = self._recheck(result, user, docs, relations, before_emit)
        return self._finish(result, start)

    def _document_result(self, result, answer, user, docs, relations, before_emit):
        sources = answer.pop("_document_sources", [])
        used = answer.pop("_document_relations", [])
        generation = answer.pop("_generation", None)
        if generation:
            result["_snapshot"]["generation"] = generation
            result["_snapshot"]["model"] = generation["model"]
            result["mode"] = "hybrid_llm"
        result.update(answer)
        result["_snapshot"]["sources"] = [
            {
                key: doc[key]
                for key in (
                    "id",
                    "title",
                    "version",
                    "section",
                    "section_text",
                    "checksum",
                    "effective_from",
                    "effective_to",
                )
            }
            for doc in sources
        ]
        result["_snapshot"]["policy_relations"] = used
        result["_snapshot"]["config"]["document_retrieval"] = {
            "mode": (
                "semantic_parent_child"
                if answer.get("trace", {}).get("hybrid")
                else "verbatim_excerpt"
            )
        }
        result["steps"][1].update(
            state="done",
            detail="Chỉ tìm trong tài liệu tổng quát đã duyệt/công bố, đúng quyền đọc và phạm vi/hiệu lực.",
        )
        result["steps"][2].update(
            state="done" if result["status"] == "answered" else "warning",
            detail="Tìm theo tên và đoạn/mục; không coi trùng tên tài liệu là đủ căn cứ, không tự chọn phiên bản mới nhất.",
        )
        if generation and generation.get("strategy") == "grounded_synthesis":
            result["steps"][2].update(
                detail="Hybrid tìm nguồn và mở đủ parent; LLM diễn đạt từng ý, kiểm số/mã và đối chiếu từng ý với các câu nguồn được dẫn."
            )
        return self._recheck(result, user, docs, relations, before_emit)

    def _authorize_generation(self, user, docs, relations):
        # Immediately before sending any text to the local model, consult SQL again.
        with connect(self.db_path) as conn:
            current_user = user_record(conn, user["id"])
            current_docs = document_records(conn)
            current_relations = [
                dict(r) for r in conn.execute("SELECT * FROM relations WHERE approved=1")
            ]
            bindings = bindings_for(conn)
        if (
            not current_user
            or fingerprint(current_user) != fingerprint(user)
            or fingerprint(current_docs) != fingerprint(docs)
            or current_relations != relations
            or not can_action(current_user, "query.ask", bindings)
        ):
            raise IndexUnavailable("Quyền hoặc nguồn đổi trước khi gọi model; chưa phát kết luận.")

    def _recheck(self, result, user, docs, relations, before_emit=None):
        # Test hook makes revocation between retrieval and emission reproducible without a public admin endpoint.
        if before_emit:
            before_emit()
        with connect(self.db_path) as conn:
            current_user = user_record(conn, user["id"])
            current_docs = {doc["id"]: doc for doc in document_records(conn)}
            current_relations = [
                dict(row) for row in conn.execute("SELECT * FROM relations WHERE approved=1")
            ]
            current_bindings = bindings_for(conn)
        stale = (
            not current_user
            or fingerprint(current_user) != fingerprint(user)
            or current_relations != relations
        )
        hybrid = result.get("trace", {}).get("hybrid")
        if hybrid and not HybridRetriever(self.db_path).verify(hybrid):
            stale = True
        if current_user and not can_action(current_user, "query.ask", current_bindings):
            stale = True
        evidence_ids = set(result["trace"]["retrieved_ids"]) | {
            item["id"] for item in result["citations"]
        }
        originals = {doc["id"]: doc for doc in docs}
        for source_id in evidence_ids:
            doc = current_docs.get(source_id)
            if (
                not doc
                or not can_read(current_user or {"groups": [], "id": ""}, doc)
                or fingerprint(doc) != fingerprint(originals[source_id])
            ):
                stale = True
        # A newly changed mandatory policy must invalidate the answer even if it was not retrieved earlier.
        if fingerprint(current_docs) != fingerprint(originals):
            stale = True
        if stale:
            result.update(
                status="insufficient_evidence",
                answer="Dữ liệu hoặc quyền truy cập đã thay đổi trong lúc xử lý. Chưa phát kết luận; vui lòng thử lại.",
                citations=[],
                amount=None,
                unit=None,
            )
            result["trace"] = {
                "policy_key": result["trace"]["policy_key"],
                "retrieved_ids": [],
                "selected_ids": [],
                "retrieval": [],
                "sources_sent_to_llm": [],
                "relations_used": [],
            }
            result["steps"][2].update(
                state="warning", detail="Chặn kết quả vì dữ liệu/quyền đã thay đổi."
            )
            result["steps"][3].update(
                state="warning", detail="Chưa phát đáp án có dữ liệu phụ thuộc."
            )
            result["_snapshot"]["sources"] = []
            result["_snapshot"]["policy_relations"] = []
            result["_snapshot"].pop("generation", None)
            result.pop("generation", None)
            result.pop("claims", None)
            result["_snapshot"]["permission_changed"] = True
        else:
            result["steps"][3].update(
                state="done", detail="Đã kiểm citation và quyền hiện hành lần cuối."
            )
        return result

    def _finish(self, result: dict, start: float) -> dict:
        snapshot = result.pop("_snapshot", {})
        normalization = snapshot.get("query_normalization", {})
        if normalization.get("changes"):
            result["trace"]["query_normalization"] = normalization
        snapshot["mode"] = result["mode"]
        result["status_label"] = STATUS_LABELS[result["status"]]
        result["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 2)
        with connect(self.db_path) as conn:
            revision = conn.execute(
                "SELECT value FROM settings WHERE key='dataset_revision'"
            ).fetchone()[0]
            snapshot["dataset_revision"] = revision
            digest = hashlib.sha256()
            for path in [ROOT / "server.py", *sorted((ROOT / "app").glob("*.py"))]:
                digest.update(path.read_bytes())
            snapshot["code_revision"] = digest.hexdigest()[:12]
            conn.execute(
                "INSERT INTO query_runs VALUES(?,?,?,?,?)",
                (
                    result["request_id"],
                    result["user"]["id"],
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(result, ensure_ascii=False),
                    json.dumps(snapshot, ensure_ascii=False),
                ),
            )
        return result
