"""Resolve what level a policy question asks about without changing read identity."""

import re
from decimal import Decimal, InvalidOperation


def money_review(question, fold):
    text = fold(question).strip(" .?!")
    match = re.fullmatch(
        r"(?:(?:khong phai|phai|toi nho|toi tuong|minh nho|minh tuong|co phai)(?: la)?\s+)?"
        r"(\d+(?:[.,]\d+)*)\s*(k|nghin|ngan|trieu|dong)(?:\s*/\s*(?:ca|ky))?"
        r"(?:\s+(?:chu|ma|dung(?: khong| ko)?|phai khong|ha|a|ah))?",
        text,
    )
    if not match:
        return None
    number, unit = match.groups()
    number = (
        re.sub(r"[.,]", "", number)
        if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", number)
        else number.replace(",", ".")
    )
    try:
        value = Decimal(number) * (
            {"k": 1000, "nghin": 1000, "ngan": 1000, "trieu": 1000000}.get(unit, 1)
        )
    except InvalidOperation:
        return None
    return int(value) if value == int(value) and 0 <= value <= 10**12 else None


def resolve_scope(question, user, organizations, fold):
    text = fold(question)
    companies = [o for o in organizations if o["level"] == "company"]
    departments = [o for o in organizations if o["level"] == "department"]
    named_companies = [
        o for o in companies if re.search(r"\b" + re.escape(fold(o["name"])) + r"\b", text)
    ]
    named_departments = [
        o for o in departments if re.search(r"\b" + re.escape(fold(o["name"])) + r"\b", text)
    ]
    group = bool(re.search(r"\btap doan\b", text))
    if (
        len(named_companies) > 1
        or len(named_departments) > 1
        or (group and (named_companies or named_departments))
    ):
        return None
    if named_departments:
        department = named_departments[0]
        if named_companies and named_companies[0]["id"] != department["parent_id"]:
            return None
        return {
            "level": "department",
            "company_id": department["parent_id"],
            "department_id": department["id"],
            "label": department["name"],
        }
    if named_companies:
        company = named_companies[0]
        if re.search(r"\b(?:phong|phong ban|bo phan)\b", text):
            return None  # Company alone does not identify which department is meant.
        return {
            "level": "company",
            "company_id": company["id"],
            "department_id": None,
            "label": company["name"],
        }
    if group:
        return {"level": "group", "company_id": None, "department_id": None, "label": "Tập đoàn"}
    if re.search(r"\bcong ty (?:cua )?(?:toi|minh)\b", text):
        if user.get("organization_level") == "group":
            return None  # A group officer is not silently assigned a subsidiary company.
        return {
            "level": "company",
            "company_id": user["company_id"],
            "department_id": None,
            "label": user["company_name"],
        }
    if re.search(r"\b(?:cong ty|phong ban|bo phan|phong)\b", text):
        return None  # Unknown/unnamed organization must not silently become the user's unit.
    # Accent folding conflates "tối" (evening) and "tôi" (I). Remove the meal phrase
    # before detecting a personal pronoun, so a bare meal question still asks scope.
    personal_text = fold(question.casefold().replace("tối", "evening"))
    personal_text = re.sub(r"\b(?:an|bua) toi\b", "evening", personal_text)
    if re.search(r"\b(?:toi|minh|ban)\b", personal_text) and not re.search(
        r"\bkhong hoi (?:quyen loi )?(?:cua )?toi\b", personal_text
    ):
        return {
            "level": "personal",
            "company_id": user["company_id"],
            "department_id": user["department_id"],
            "label": " · ".join(
                v for v in (user["name"], user["company_name"], user["department_name"]) if v
            ),
        }
    return None


def short_scope_reply(question, organizations, fold):
    text = fold(question).strip(" .?!")
    return text in {"tap doan", "theo ho so cua toi", "quyen loi cua toi", "cong ty cua toi"} | {
        fold(o["name"]) for o in organizations if o["level"] in ("company", "department")
    }


def scope_documents(scope, docs):
    if scope["level"] == "personal":
        return docs
    return [
        d
        for d in docs
        if d["applies_company"] == scope["company_id"]
        and d["applies_department"] == scope["department_id"]
    ]


def lookup_context(scope, user):
    # Only applicability changes for an explicit lookup; ACL always uses the original user.
    if scope["level"] == "personal":
        return user
    return {
        **user,
        "company_id": scope["company_id"],
        "department_id": scope["department_id"],
        "_policy_lookup": True,
    }
