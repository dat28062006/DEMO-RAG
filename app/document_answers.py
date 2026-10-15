"""Lexical passage retrieval for general documents; source text is never executed."""

import re
import unicodedata
from datetime import date

from .store import applicable, can_read, query_candidates, scope_matches, validity_info

COMMON = set(
    "tai lieu chinh sach quy dinh huong dan cong ty noi bo tri thuc toi ban gi nao noi ve gom hay cho biet trinh bay mo ta tim mo xem file pdf docx find open document".split()
)


def title_key(text):
    text = unicodedata.normalize("NFD", text.lower().replace("đ", "d"))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return " ".join(re.findall(r"[^\W_]+", text))


def passages(doc):
    text = doc["section_text"]
    headings = list(
        re.finditer(r"(?m)^(?:#{1,6}\s+\S[^\n]*|\d+(?:\.\d+)*[.)]\s+\S[^\n]{2,110})$", text)
    )
    boundaries = sorted({0, len(text), *[h.start() for h in headings]})
    for index in range(len(boundaries) - 1):
        begin, end = boundaries[index : index + 2]
        # Bound excerpts without cutting or rewriting the source text.
        while begin < end:
            limit = min(end, begin + 2200)
            if limit < end:
                split = text.rfind("\n", begin, limit)
                if split > begin:
                    limit = split + 1
            raw = text[begin:limit]
            quote = raw.strip()
            if quote:
                start = begin + len(raw) - len(raw.lstrip())
                line = text.count("\n", 0, start) + 1
                heading = next(
                    (h.group().strip("# ") for h in reversed(headings) if h.start() <= start),
                    doc["section"],
                )
                yield {
                    "id": f"{doc['id']}:{start}",
                    "title": heading,
                    "section_text": quote,
                    "doc": doc,
                    "line_start": line,
                    "line_end": line + quote.count("\n"),
                    "offset": start,
                }
            begin = limit


def answer_documents(
    user, docs, relations, question, as_of, tokens, bm25, citation, explicit_only=False
):
    def reading_context(doc):
        organization = doc.get("applies_department") or doc.get("applies_company")
        if organization in user.get("organization_read_scope", {}).get(
            "allowed_ids", []
        ) and not doc.get("facts", {}).get("subject_id"):
            # A general document lookup is not a claim about the reader's own benefits.
            return {
                **user,
                "company_id": doc["applies_company"],
                "department_id": doc["applies_department"],
                "role": doc["applies_role"] or user["role"],
            }
        return user

    eligible = [
        d
        for d in docs
        if d["policy_key"] == "general"
        and d in query_candidates(reading_context(d), docs, relations, as_of)
    ]
    readable = [d for d in eligible if can_read(user, d)]
    references = [
        d
        for d in docs
        if d["policy_key"] == "general"
        and scope_matches(reading_context(d), d)
        and can_read(user, d)
    ]
    query = set(tokens(question)) - COMMON
    named = []
    for doc in references:
        title_terms = set(tokens(doc["title"])) - COMMON
        shared = query & title_terms
        if len(shared) >= 2 or any(
            len(word) >= 6
            and len({title_key(d["title"]) for d in references if word in set(tokens(d["title"]))})
            == 1
            for word in shared
        ):
            named.append(doc)
    if explicit_only and not named:
        return None
    candidates = [d for d in named if d in readable] if named else readable
    if not candidates and not named:
        return None
    # Remove the document title from passage matching so naming a file alone is not evidence.
    content_query = (
        query - set().union(*(set(tokens(d["title"])) for d in named)) if named else query
    )
    base = {
        "trace": {
            "policy_key": "general",
            "retrieved_ids": [],
            "selected_ids": [],
            "retrieval": [],
            "sources_sent_to_llm": [],
            "relations_used": [],
        },
        "amount": None,
        "unit": None,
        "citations": [],
        "_document_sources": [],
        "_document_relations": [],
    }

    def missing():
        return {
            **base,
            "status": "insufficient_evidence",
            "reason": "document_passage_not_found",
            "answer": "Chưa tìm được đoạn đủ khớp với điều bạn hỏi trong các tài liệu được phép sử dụng. Bạn hãy nêu rõ mục hoặc nội dung cần tra cứu; tôi chưa có căn cứ để trả lời.",
        }

    dates = set(re.findall(r"\b\d{1,2}/\d{1,2}/\d{4}\b|\b\d{4}-\d{2}-\d{2}\b", question))
    if len(dates) > 1:
        return {
            **base,
            "status": "unsupported",
            "answer": "MVP hiện chỉ xử lý một thời điểm áp dụng mỗi lần hỏi. Hãy tách câu hỏi theo từng thời điểm.",
        }
    if dates:
        mentioned = next(iter(dates))
        try:
            normalized_date = (
                date(*reversed(list(map(int, mentioned.split("/"))))).isoformat()
                if "/" in mentioned
                else date.fromisoformat(mentioned).isoformat()
            )
        except ValueError:
            normalized_date = None
        if normalized_date != as_of:
            return {
                **base,
                "status": "needs_context",
                "answer": "Vui lòng chọn lại ngày áp dụng khớp với thời điểm trong câu hỏi trước khi tra cứu tài liệu.",
            }
    if named and not candidates:
        states = sorted(
            {validity_info(d, as_of, user, docs, relations)["validity_label"] for d in named}
        )
        return {
            **base,
            "status": "insufficient_evidence",
            "reason": "document_not_effective",
            "answer": f"Tài liệu được phép đọc khớp tên đang ở trạng thái: {', '.join(states)} tại ngày {as_of}. Không dùng làm căn cứ cho ngày này. Bạn có thể chọn ngày tra cứu lịch sử hoặc bổ sung phiên bản đã duyệt/công bố và còn hiệu lực.",
        }
    if len(content_query) < 2:
        if not named:
            return None
        if not content_query and not re.search(
            r"\b(?:noi gi|tom tat|giai thich|co gi)\b", title_key(question)
        ):
            from .document_lookup import is_file_lookup

            if not is_file_lookup(question):
                content_query = query
        if not content_query and re.match(
            r"^(?:mo|xem|tai lieu|file|tim (?:tai lieu|file))\b", title_key(question)
        ):
            ids = {d["id"] for d in candidates}
            used = [r for r in relations if r["source_id"] in ids and r["target_id"] in ids]
            replaced = {r["target_id"] for r in used if r["type"] == "supersedes"}
            found = [d for d in candidates if d["id"] not in replaced]
            if not found:
                return missing()
            conflict = len(found) > 1 and len({title_key(d["title"]) for d in found}) == 1
            return {
                **base,
                "status": "conflict" if conflict else "answered",
                "answer_kind": "document_lookup",
                "answer": (
                    "Có nhiều phiên bản cùng hiệu lực chưa có quan hệ ưu tiên được duyệt. Bạn có thể mở từng nguồn để đối chiếu; tôi chưa chọn một bản làm căn cứ."
                    if conflict
                    else "Tôi tìm thấy tài liệu bạn được phép đọc:\n"
                    + "\n".join(f"• {d['title']} — {d['id']} · v{d['version']}" for d in found[:5])
                    + "\n\nBấm nguồn bên dưới để đọc tài liệu hoặc tải file gốc nếu có."
                ),
                "citations": [citation(d) for d in found[:5]],
                "_document_sources": found,
                "_document_relations": used,
                "trace": {
                    **base["trace"],
                    "retrieved_ids": [d["id"] for d in found],
                    "selected_ids": [d["id"] for d in found],
                    "relations_used": used,
                },
            }
        if len(content_query) < 2:
            return {
                **base,
                "status": "needs_context",
                "clarification_kind": "document_question",
                "answer": "Bạn muốn tìm nội dung hoặc mục nào trong tài liệu này? Hãy nói rõ điều cần biết để tôi tìm đúng đoạn có căn cứ.",
            }
    chunks = [part for doc in candidates for part in passages(doc)]
    ranked = bm25(" ".join(content_query), chunks)
    relevant = []
    quoted = [
        title_key(s)
        for s in re.findall(r'["\u201c]([^"\u201d]+)["\u201d]', question)
        if title_key(s) not in {title_key(d["title"]) for d in named}
    ]
    for chunk, score in ranked:
        if any(phrase not in title_key(chunk["section_text"]) for phrase in quoted):
            continue
        matched = content_query & set(tokens(chunk["section_text"]))
        # A title match or a couple of common words must not produce a confident answer.
        if len(matched) >= 2 and len(matched) / len(content_query) >= 0.7:
            heading_match = len(content_query & set(tokens(chunk["title"]))) / len(content_query)
            relevant.append((chunk, score + 10 * heading_match))
    if not relevant:
        return missing() if named else None
    relevant.sort(key=lambda item: (-item[1], item[0]["id"]))
    by_doc = {}
    for chunk, score in relevant:
        by_doc.setdefault(chunk["doc"]["id"], (chunk, score))
    families = {title_key(entry[0]["doc"]["title"]) for entry in by_doc.values()}
    matching_docs = [doc for doc in candidates if title_key(doc["title"]) in families]
    # A hidden active version in a matching family prevents a silent fallback.
    if any(
        title_key(doc["title"]) in families
        and doc["status"] == "published"
        and doc["approval"] == "approved"
        and not can_read(user, doc)
        for doc in eligible
    ):
        return missing()
    matching_ids = {doc["id"] for doc in matching_docs}
    used = [
        r
        for r in relations
        if r["source_id"] in matching_ids
        and r["target_id"] in matching_ids
        and r["type"] in ("supersedes", "overrides")
    ]
    replaced = {r["target_id"] for r in used}
    winners = [doc for doc in matching_docs if doc["id"] not in replaced]
    if not winners:
        return missing()
    selected = [by_doc[doc["id"]] for doc in winners if doc["id"] in by_doc]
    selected.sort(key=lambda item: (-item[1], item[0]["id"]))
    base["trace"].update(
        retrieved_ids=[doc["id"] for doc in matching_docs],
        selected_ids=[doc["id"] for doc in winners],
        retrieval=[
            {
                "id": part["doc"]["id"],
                "score": round(score, 5),
                "line_start": part["line_start"],
                "line_end": part["line_end"],
            }
            for part, score in selected[:5]
        ],
        relations_used=used,
    )
    base["_document_sources"] = matching_docs
    base["_document_relations"] = used

    def cite(part):
        result = citation(part["doc"], part["section_text"])
        result.update(
            location=part["title"],
            line_start=part["line_start"],
            line_end=part["line_end"],
            start_offset=part["offset"],
            end_offset=part["offset"] + len(part["section_text"]),
        )
        return result

    if len(winners) > 1:
        if len({title_key(doc["title"]) for doc in winners}) == 1:
            return {
                **base,
                "status": "conflict",
                "reason": "document_versions_unresolved",
                "answer": "Có nhiều phiên bản cùng hiệu lực của tài liệu này nhưng chưa có quan hệ thay thế được duyệt. Tôi chưa thể chọn bản làm căn cứ.",
                "citations": [cite(part) for part, _ in selected[:5]],
            }
        return {
            **base,
            "status": "needs_context",
            "clarification_kind": "document_selection",
            "answer": "Có nhiều tài liệu được phép đọc chứa nội dung liên quan. Bạn muốn tra cứu tài liệu nào?",
            "follow_up_options": [
                {"label": doc["title"], "question": question + " — tài liệu " + doc["title"]}
                for doc in winners[:5]
            ],
        }
    if not selected:
        return missing()
    part = selected[0][0]
    # Quantitative queries need a quantitative excerpt; do not infer a number from unrelated text.
    if re.search(r"\b(bao nhieu|may|how many)\b", title_key(question)) and not re.search(
        r"\d", part["section_text"].split("\n", 1)[-1]
    ):
        return missing()
    return {
        **base,
        "status": "answered",
        "answer_kind": "source_excerpt",
        "answer": f"Theo tài liệu \u201c{part['doc']['title']}\u201d, đoạn liên quan đến câu hỏi là:\n\n{part['section_text']}",
        "citations": [cite(part)],
    }
