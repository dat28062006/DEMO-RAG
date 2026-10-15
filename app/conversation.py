"""Small, explicit dialogue state for leave questions; never a source of rights or facts."""

from __future__ import annotations

import re
import unicodedata
from datetime import date


def fold(text: str) -> str:
    text = unicodedata.normalize("NFD", text.casefold().replace("đ", "d"))
    return " ".join("".join(c for c in text if unicodedata.category(c) != "Mn").split())


def leave_type(text: str) -> str | None:
    for label, pattern in (
        ("nghỉ phép năm", r"\b(?:nghi phep|phep nam)\b"),
        ("nghỉ lễ", r"\b(?:nghi le|ngay le|nghi tet)\b"),
        ("nghỉ ốm", r"\b(?:nghi om|om dau)\b"),
        ("nghỉ thai sản", r"\b(?:thai san|nghi sinh|sinh con|nghi de)\b"),
    ):
        if re.search(pattern, text):
            return label
    return None


def period_from(text: str, as_of: str) -> str | None:
    reference = date.fromisoformat(as_of)
    if re.search(r"\bnam nay\b", text):
        return f"năm {reference.year}"
    if re.search(r"\bnam ngoai\b", text):
        return f"năm {reference.year - 1}"
    if re.search(r"\bthang nay\b", text):
        return f"tháng {reference.month}/{reference.year}"
    if re.search(r"\b(?:hien tai|den nay|toi nay)\b", text):
        return f"tính đến {as_of}"
    month = re.search(r"\bthang\s+(\d{1,2})(?:\s*/\s*(\d{4})|\s+nam\s+(\d{4}))?\b", text)
    if month and 1 <= int(month[1]) <= 12:
        return f"tháng {int(month[1])}/{month[2] or month[3] or reference.year}"
    year = re.search(r"\bnam\s+(20\d{2})\b", text)
    if year:
        return f"năm {year[1]}"
    dates = re.findall(r"\b\d{1,2}/\d{1,2}/\d{4}\b|\b\d{4}-\d{2}-\d{2}\b", text)
    if dates and len(dates) <= 2:
        try:
            values = [
                (
                    date(*map(int, value.split("/")[::-1]))
                    if "/" in value
                    else date.fromisoformat(value)
                )
                for value in dates
            ]
        except ValueError:
            return None
        if len(values) == 2 and values[0] <= values[1]:
            return f"từ {values[0].isoformat()} đến {values[1].isoformat()}"
        if len(values) == 1:
            return f"tính đến {values[0].isoformat()}"
    return None


def leave_dialogue(question: str, as_of: str, previous: dict | None = None) -> dict | None:
    text = fold(question)
    kind = leave_type(text)
    prior = previous if previous and previous.get("topic") == "leave" else None
    # Inherit only in this topic or a narrow reply to a pending slot, never arbitrary new topics.
    short_reply = bool(
        prior
        and (
            re.fullmatch(
                r"(?:trong |tinh |cho )?(?:nam nay|nam ngoai|thang nay|nam 20\d{2}|thang \d{1,2}(?:/\d{4})?|hien tai)",
                text,
            )
            or re.fullmatch(r"(?:nghi )?(?:phep(?: nam)?|le|om)", text)
            or re.fullmatch(
                r"(?:toi )?(?:con|da nghi|duoc nghi) bao nhieu(?: ngay)?(?: roi| r)?[?.!]*", text
            )
        )
    )
    if not (
        kind
        or re.search(r"\bnghi\b", text)
        or re.search(r"\b(?:ngay phep|phep con lai|da (?:su dung|dung) phep)\b", text)
        or short_reply
    ):
        return None
    if re.search(r"\bnghi viec\b", text):
        return None
    used = bool(
        re.search(r"\bda\s+nghi\b|\bnghi\b.*\b(?:roi|r)\b|\bda (?:su dung|dung)\b.*\bphep\b", text)
    )
    balance_text = re.sub(r"\bsinh con\b", "maternity", text)
    remaining = bool(
        re.search(
            r"\bcon\b.*\b(?:nghi|phep|ngay|bao nhieu)\b|\b(?:nghi|phep)\b.*\bcon lai\b",
            balance_text,
        )
    )
    explicit_entitlement = bool(re.search(r"\bduoc nghi\b|\bquy dinh\b|\bche do\b", text))
    intent = (
        "balance"
        if remaining
        else (
            "usage"
            if used
            else (
                "entitlement"
                if explicit_entitlement
                else prior["intent"] if prior else "entitlement"
            )
        )
    )
    current_period = period_from(text, as_of)
    # Selecting another kind or explicitly switching intent should not reuse an old reporting period.
    changed = bool(
        prior and (intent != prior["intent"] or (kind and kind != prior.get("leave_type")))
    )
    frame = {
        "topic": "leave",
        "intent": intent,
        "leave_type": kind or (prior.get("leave_type") if prior else None),
        "period": current_period or (prior.get("period") if prior and not changed else None),
    }
    if short_reply and not kind and re.fullmatch(r"(?:nghi )?(?:phep(?: nam)?|le|om)", text):
        frame["leave_type"] = {"phep": "nghỉ phép năm", "le": "nghỉ lễ", "om": "nghỉ ốm"}.get(
            text.removeprefix("nghi "), "nghỉ phép năm"
        )
    label = frame["leave_type"] or "nghỉ"
    if intent in ("usage", "balance"):
        target = "số ngày đã nghỉ" if intent == "usage" else "số ngày phép còn lại"
        if not frame["period"]:
            return {
                "status": "needs_context",
                "reason": "question_needs_clarification",
                "clarification_kind": "leave_period",
                "conversation_state": frame,
                "answer": f"Bạn muốn tính {target} trong năm nào, tháng nào hoặc khoảng thời gian nào?"
                + (f" Tôi giữ loại nghỉ bạn đã chọn: {label}." if frame["leave_type"] else "")
                + " Cần kỳ tính để đối chiếu nguồn lịch sử nghỉ phép/chấm công cá nhân phù hợp.",
                "follow_up_options": [
                    {"label": "Năm nay", "question": "Năm nay"},
                    {"label": "Tháng này", "question": "Tháng này"},
                ],
            }
        return {
            "status": "insufficient_evidence",
            "reason": "personal_records_unavailable",
            "conversation_state": frame,
            "answer": f'Bạn đang hỏi {target} ({label}, {frame["period"]}). Chưa tìm thấy nguồn nghỉ phép/chấm công cá nhân được phép dùng, đã công bố và khớp kỳ dữ liệu của bạn'
            + (" và số dư phép được xác nhận" if intent == "balance" else "")
            + ". Tôi không thể tính con số này từ tài liệu chính sách hoặc đoán theo hội thoại. Cần dữ liệu cá nhân được xác thực và cấp quyền truy cập.",
            "follow_up_options": [],
        }
    if not frame["leave_type"]:
        return {
            "status": "needs_context",
            "reason": "question_needs_clarification",
            "clarification_kind": "leave_type",
            "conversation_state": frame,
            "answer": "Bạn muốn hỏi số ngày nghỉ phép năm, nghỉ lễ hay một chế độ nghỉ khác? Bạn có thể chọn bên dưới hoặc nói rõ loại nghỉ muốn tra cứu.",
            "follow_up_options": [
                {"label": name, "question": name}
                for name in ("Nghỉ phép năm", "Nghỉ lễ", "Nghỉ ốm")
            ],
        }
    return {
        "status": "insufficient_evidence",
        "reason": "policy_not_available",
        "conversation_state": frame,
        "answer": f"Bạn đang hỏi về quyền được {label}. Bản demo hiện chưa hỗ trợ tra cứu chính sách này và chưa có nguồn được phép dùng để xác định quyền lợi. Tôi chưa thể trả lời số ngày; cần bổ sung tài liệu được duyệt và công bố trong phạm vi của bạn.",
        "follow_up_options": [],
    }
