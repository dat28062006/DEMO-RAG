"""Find readable files by their title or content; finding a file is not answering facts."""

import re
from datetime import date
from .document_answers import title_key
from .store import can_read, query_candidates, validity_info

LOOKUP_STOP = set(
    "tim mo xem cho toi giup minh tai lieu file document pdf docx ve cua va hay mot cac trong".split()
)


def lookup_words(text):
    # Orthographic variants for lookup only; source text and embedding revision stay unchanged.
    text = re.sub(r"\bquan li\b", "quan ly", title_key(text))
    return set(text.split()) - LOOKUP_STOP


def is_file_lookup(question):
    text = title_key(question)
    if not re.match(
        r"^(?:tim|mo|xem|tai lieu|file|cho (?:toi|minh) (?:tim|xem)|hay (?:tim|mo))\b", text
    ):
        return False
    if not re.search(r"\b(tai lieu|file|document|pdf|docx)\b", text) and not re.match(
        r"^(mo|xem|hay mo|cho (toi|minh) xem)\b", text
    ):
        return False
    # A request to find a quantitative answer or explain a step still goes to question answering.
    return not re.search(
        r"\b(?:bao nhieu|may ngay|the nao|la gi|cac buoc|tom tat|giai thich|so sanh|tim hieu|tim cau tra loi|noi dung|buoc \d+)\b",
        text,
    )


def lookup_documents(user, docs, relations, question, as_of, citation, retriever=None):
    if not is_file_lookup(question):
        return None
    query = lookup_words(question)
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
        "answer_kind": "document_lookup",
    }

    def no_result():
        return {
            **base,
            "status": "insufficient_evidence",
            "reason": "file_not_found",
            "answer": "Chưa tìm thấy tài liệu phù hợp trong phạm vi bạn được phép đọc. Tôi chưa có nguồn để mở; bạn có thể bổ sung tên, mã hoặc chủ đề tài liệu.",
        }

    if not query:
        return {
            **base,
            "status": "needs_context",
            "answer": "Bạn muốn tìm tài liệu về chủ đề nào? Có thể nêu tên, mã hoặc nội dung cần tìm.",
        }
    dates = set(re.findall(r"\b\d{1,2}/\d{1,2}/\d{4}\b|\b\d{4}-\d{2}-\d{2}\b", question))
    if len(dates) > 1:
        return {
            **base,
            "status": "unsupported",
            "answer": "Hãy tìm theo từng thời điểm để đối chiếu đúng phiên bản.",
        }
    if dates:
        value = next(iter(dates))
        try:
            day = (
                date(*reversed(list(map(int, value.split("/"))))).isoformat()
                if "/" in value
                else date.fromisoformat(value).isoformat()
            )
        except ValueError:
            day = None
        if day != as_of:
            return {
                **base,
                "status": "needs_context",
                "answer": "Ngày trong câu hỏi khác ngày áp dụng. Hãy xác nhận ngày tra cứu trước khi chọn phiên bản.",
            }
        query -= lookup_words(value)
    # Reading a file is distinct from applying its policy to the reader's own benefits.
    readable = [d for d in docs if can_read(user, d)]
    candidates = []
    for doc in readable:
        context = {
            **user,
            "company_id": doc["applies_company"] or user["company_id"],
            "department_id": doc["applies_department"],
            "role": doc["applies_role"] or user["role"],
        }
        if doc in query_candidates(context, docs, relations, as_of):
            candidates.append(doc)

    def match(doc):
        title = lookup_words(doc["title"])
        body = lookup_words(doc["section_text"])
        title_shared = query & title
        body_shared = query & body
        title_coverage = len(title_shared) / len(query)
        body_coverage = len(body_shared) / len(query)
        unique = any(
            len(w) >= 6 and sum(w in lookup_words(d["title"]) for d in readable) == 1
            for w in title_shared
        )
        # Return file candidates, never treat this coverage as proof of a factual answer.
        if title_coverage >= 0.6 and (len(title_shared) >= 2 or unique):
            return 2 + title_coverage
        if body_coverage >= 0.8 and len(body_shared) >= 2:
            return body_coverage
        return 0

    ranked = sorted(
        [(d, match(d)) for d in candidates if match(d)], key=lambda item: (-item[1], item[0]["id"])
    )
    if not ranked and retriever and retriever.enabled and candidates:
        # Hybrid content discovery uses the full request, preserving the requested subject.
        parents = retriever.rank_parents(question, candidates)
        by_id = {}
        for part, score in parents:
            if part["dense_score"] >= 0.62 or (
                part["dense_score"] >= 0.5
                and len(query & lookup_words(part["section_text"])) / len(query) >= 0.6
            ):
                by_id.setdefault(part["doc"]["id"], (part["doc"], score))
        ranked = list(by_id.values())
        base["trace"]["hybrid"] = retriever.last_trace
    if not ranked:
        historical = [d for d in readable if match(d)]
        if historical:
            states = sorted(
                {
                    validity_info(d, as_of, user, docs, relations)["validity_label"]
                    for d in historical
                }
            )
            return {
                **base,
                "status": "insufficient_evidence",
                "reason": "document_not_effective",
                "answer": "Tài liệu khớp đang ở trạng thái "
                + ", ".join(states)
                + f" tại ngày {as_of}. Không chọn làm nguồn hiện hành; bạn có thể tra cứu lịch sử trong thư viện.",
            }
        return no_result()
    found = [d for d, _ in ranked]
    ids = {d["id"] for d in found}
    used = [r for r in relations if r["source_id"] in ids and r["target_id"] in ids]
    # A department exception is another readable file, not a replacement version.
    replaced = {r["target_id"] for r in used if r["type"] == "supersedes"}
    found = [d for d in found if d["id"] not in replaced][:5]
    conflict = len({title_key(d["title"]) for d in found}) < len(found)
    base["trace"].update(
        retrieved_ids=sorted(ids),
        selected_ids=[d["id"] for d in found],
        retrieval=[{"id": d["id"], "score": score} for d, score in ranked[:5]],
        relations_used=used,
    )
    return {
        **base,
        "status": "conflict" if conflict else "answered",
        "answer": (
            "Tìm thấy các phiên bản cùng hiệu lực chưa có quan hệ ưu tiên đã duyệt. Bạn có thể mở để đối chiếu; tôi chưa chọn một bản làm căn cứ."
            if conflict
            else "Tôi tìm thấy tài liệu bạn được phép đọc:\n"
            + "\n".join(f"• {d['title']} — {d['id']} · v{d['version']}" for d in found)
            + "\n\nBấm nguồn bên dưới để đọc hoặc tải file gốc."
        ),
        "citations": [citation(d) for d in found],
        "_document_sources": [d for d, _ in ranked],
        "_document_relations": used,
    }
