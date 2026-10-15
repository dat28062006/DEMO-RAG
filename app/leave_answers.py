"""Answer only from imported, published leave sources with explicit scope and period."""

import re
from calendar import monthrange
from datetime import date

from .importer import leave_value
from .store import applicable, can_read, query_candidates


def period_bounds(period, as_of):
    if not period:
        return None
    if match := re.fullmatch(r"năm (\d{4})", period):
        year = int(match[1])
        return f"{year}-01-01", min(f"{year}-12-31", as_of)
    if match := re.fullmatch(r"tháng (\d{1,2})/(\d{4})", period):
        month, year = int(match[1]), int(match[2])
        return date(year, month, 1).isoformat(), min(
            date(year, month, monthrange(year, month)[1]).isoformat(), as_of
        )
    if match := re.fullmatch(r"từ (\d{4}-\d{2}-\d{2}) đến (\d{4}-\d{2}-\d{2})", period):
        return match[1], match[2]
    if match := re.fullmatch(r"tính đến (\d{4}-\d{2}-\d{2})", period):
        return match[1][:4] + "-01-01", match[1]
    return None


def answer_leave(user, docs, relations, dialogue, as_of, cite):
    if dialogue["status"] == "needs_context":
        return None
    frame = dialogue["conversation_state"]
    kind = frame["intent"]
    if kind == "entitlement" and frame["period"] and frame["period"] != f"năm {as_of[:4]}":
        return {
            "status": "needs_context",
            "reason": "date_needs_confirmation",
            "answer": "Bạn đang hỏi chính sách nghỉ ở thời kỳ khác ngày áp dụng đang chọn. Hãy chọn ngày áp dụng trong thời kỳ muốn tra cứu để tôi kiểm đúng phiên bản.",
            "follow_up_options": [],
        }
    eligible = []
    for doc in query_candidates(user, docs, relations, as_of):
        facts = doc.get("facts", {})
        if (
            doc["policy_key"] != "leave"
            or facts.get("kind") != kind
            or not applicable(user, doc, as_of)
        ):
            continue
        if doc["status"] == "staging" or doc["approval"] != "approved":
            continue
        if frame["leave_type"] and facts.get("leave_type") != frame["leave_type"]:
            continue
        if kind != "entitlement":
            if facts.get("subject_id") != user["id"]:
                continue
            bounds = period_bounds(frame["period"], as_of)
            if not bounds or bounds != (facts.get("period_start"), facts.get("period_end")):
                continue
        eligible.append(doc)
    if not eligible:
        return None
    if any(not can_read(user, doc) for doc in eligible):
        return {
            "status": "insufficient_evidence",
            "reason": "source_not_available",
            "answer": "Chưa có đủ nguồn được phép sử dụng cho hồ sơ và kỳ cần tra cứu. Tôi chưa thể xác nhận số ngày.",
            "follow_up_options": [],
        }
    ids = {doc["id"] for doc in eligible}
    used = [r for r in relations if r["source_id"] in ids and r["target_id"] in ids]
    replaced = {r["target_id"] for r in used}
    winners = [doc for doc in eligible if doc["id"] not in replaced]
    if not winners:
        return None
    output = {
        "follow_up_options": [],
        "reason": "supported_by_imported_source",
        "_leave_sources": eligible,
        "_leave_relations": used,
        "trace": {
            "policy_key": "leave",
            "retrieved_ids": sorted(ids),
            "selected_ids": [doc["id"] for doc in winners],
            "retrieval": [{"id": doc["id"], "score": None} for doc in eligible],
            "sources_sent_to_llm": [],
            "relations_used": used,
        },
    }
    if len(winners) > 1:
        return {
            **output,
            "status": "conflict",
            "answer": "Có nhiều nguồn nghỉ phép cùng phạm vi và kỳ dữ liệu nhưng chưa có quan hệ ưu tiên được duyệt. Chưa thể xác nhận số ngày.",
            "citations": [cite(doc) for doc in winners],
        }
    winner = winners[0]
    try:
        amount, quote = leave_value(winner["section_text"], kind)
    except ValueError:
        return None
    label = winner["facts"]["leave_type"]
    target = {"entitlement": "được nghỉ", "usage": "đã nghỉ", "balance": "còn lại"}[kind]
    period = (
        f"; kỳ {winner['facts']['period_start']} → {winner['facts']['period_end']}"
        if kind != "entitlement"
        else ""
    )
    return {
        **output,
        "status": "answered",
        "amount": amount,
        "unit": "ngày/năm" if kind == "entitlement" else "ngày",
        "answer": f"Theo nguồn giả lập áp dụng cho {user['name']}: {quote} Loại nghỉ: {label}{period}.",
        "citations": [cite(winner, quote)],
    }
