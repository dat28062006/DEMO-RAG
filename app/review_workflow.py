"""Company approval sets a viewer ceiling; department approval can only narrow it."""

from __future__ import annotations

import json
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def review_record(conn, document_id):
    row = conn.execute(
        "SELECT * FROM document_reviews WHERE document_id=?", (document_id,)
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    for key in ("proposed_allow", "proposed_deny", "company_candidates", "direct_users"):
        result[key] = json.loads(result.pop(key + "_json"))
    result["departments"] = []
    for row in conn.execute(
        "SELECT r.*,o.name department_name FROM department_reviews r JOIN organizations o ON o.id=r.department_id WHERE document_id=? ORDER BY department_id",
        (document_id,),
    ):
        item = dict(row)
        item["candidates"] = json.loads(item.pop("candidates_json"))
        item["selected"] = json.loads(item.pop("selected_json"))
        result["departments"].append(item)
    return result


def enabled(conn, doc):
    row = conn.execute(
        "SELECT enabled FROM organization_review_policies WHERE organization_id=?",
        (doc.get("applies_company"),),
    ).fetchone()
    return bool(row and row[0])


def ensure_review(conn, doc, force=False, allow=None, deny=None):
    if not enabled(conn, doc):
        return None
    existing = review_record(conn, doc["id"])
    if existing and not force:
        return existing
    revision = existing["revision"] + 1 if existing else 1
    conn.execute("DELETE FROM department_reviews WHERE document_id=?", (doc["id"],))
    conn.execute(
        """INSERT INTO document_reviews(document_id,revision,stage,proposed_allow_json,proposed_deny_json,updated_at)
        VALUES(?,?,'company',?,?,?) ON CONFLICT(document_id) DO UPDATE SET revision=excluded.revision,
        stage='company',proposed_allow_json=excluded.proposed_allow_json,proposed_deny_json=excluded.proposed_deny_json,
        company_candidates_json='[]',direct_users_json='[]',company_approved_by=NULL,override_reason=NULL,updated_at=excluded.updated_at""",
        (
            doc["id"],
            revision,
            json.dumps(doc["allow"] if allow is None else allow),
            json.dumps(doc["deny"] if deny is None else deny),
            now(),
        ),
    )
    conn.execute(
        "UPDATE documents SET approval='pending',status='staging' WHERE id=?", (doc["id"],)
    )
    conn.execute("UPDATE relations SET approved=0 WHERE source_id=?", (doc["id"],))
    return review_record(conn, doc["id"])


def resolve_viewers(conn, doc, allow, deny):
    from .store import principals_for, user_record, valid_read_principals

    valid = valid_read_principals(conn)
    if (
        not isinstance(allow, list)
        or not isinstance(deny, list)
        or any(not isinstance(p, str) or p not in valid for p in allow + deny)
    ):
        raise ValueError("Danh sách quyền đọc không hợp lệ.")
    people = [user_record(conn, r[0]) for r in conn.execute("SELECT id FROM users ORDER BY id")]
    scoped = [
        u
        for u in people
        if u["company_id"] == doc["applies_company"]
        and (not doc.get("applies_department") or u["department_id"] == doc["applies_department"])
    ]
    scope_ids = {u["id"] for u in scoped}
    for principal in allow:
        if principal.startswith("user:") and principal[5:] not in scope_ids:
            raise ValueError("Không cấp người xem ngoài đơn vị áp dụng của tài liệu.")
        if principal.startswith("company:") and principal[8:] != doc["applies_company"]:
            raise ValueError("Không cấp quyền cho công ty khác.")
        if principal.startswith("department:"):
            org = conn.execute(
                "SELECT parent_id FROM organizations WHERE id=?", (principal[11:],)
            ).fetchone()
            if (
                not org
                or org[0] != doc["applies_company"]
                or (doc.get("applies_department") and principal[11:] != doc["applies_department"])
            ):
                raise ValueError("Không cấp quyền cho phòng ban ngoài phạm vi.")
        if principal in ("PUBLIC", doc["applies_company"] + "_STAFF"):
            continue
        if not principal.startswith(("user:", "company:", "department:")):
            members = [u for u in people if principal in principals_for(u)]
            if members and any(u["id"] not in scope_ids for u in members):
                raise ValueError("Nhóm người xem chứa nhân sự ngoài đơn vị áp dụng.")
    return [
        u
        for u in scoped
        if principals_for(u).intersection(allow) and not principals_for(u).intersection(deny)
    ]


def check_revision(review, payload):
    if (
        type(payload.get("expected_review_revision")) is not int
        or payload["expected_review_revision"] != review["revision"]
    ):
        raise ValueError("Luồng duyệt đã thay đổi. Hãy tải lại tài liệu trước khi duyệt.")


def approve_company(conn, doc, user, payload):
    review = review_record(conn, doc["id"])
    check_revision(review, payload)
    if doc["status"] != "staging" or review["stage"] != "company":
        raise ValueError("Tài liệu không ở bước chờ giám đốc duyệt.")
    allow = payload.get("allow", review["proposed_allow"])
    deny = payload.get("deny", review["proposed_deny"])
    people = resolve_viewers(conn, doc, allow, deny)
    if not people:
        raise ValueError("Cần ít nhất một người xem trong phạm vi công ty.")
    from .importer import validate_personal_acl

    validate_personal_acl(conn, doc, allow, user)
    candidates = [u["id"] for u in people]
    direct = [u["id"] for u in people if not u["department_id"]]
    for department in sorted({u["department_id"] for u in people if u["department_id"]}):
        ids = [u["id"] for u in people if u["department_id"] == department]
        conn.execute(
            "INSERT INTO department_reviews VALUES(?,?,?,'[]','pending',NULL)",
            (doc["id"], department, json.dumps(ids)),
        )
    conn.execute(
        "UPDATE document_reviews SET stage='departments',revision=revision+1,proposed_allow_json=?,proposed_deny_json=?,company_candidates_json=?,direct_users_json=?,company_approved_by=?,updated_at=? WHERE document_id=?",
        (
            json.dumps(allow),
            json.dumps(deny),
            json.dumps(candidates),
            json.dumps(direct),
            user["id"],
            now(),
            doc["id"],
        ),
    )
    finish_if_ready(conn, doc["id"])
    return {"candidate_user_ids": candidates, "stage": review_record(conn, doc["id"])["stage"]}


def approve_department(conn, doc, user, bindings, payload):
    from .store import can_action, user_record

    review = review_record(conn, doc["id"])
    check_revision(review, payload)
    if doc["status"] != "staging" or review["stage"] != "departments":
        raise ValueError("Giám đốc cần duyệt phạm vi trước khi trưởng phòng duyệt.")
    department = payload.get("department_id")
    row = next(
        (
            r
            for r in review["departments"]
            if r["department_id"] == department and r["status"] == "pending"
        ),
        None,
    )
    selected = payload.get("selected_user_ids")
    if (
        not row
        or not isinstance(selected, list)
        or any(not isinstance(i, str) or i not in row["candidates"] for i in selected)
    ):
        raise ValueError("Trưởng phòng chỉ chọn người trong danh sách giám đốc đã duyệt.")
    scoped_doc = {**doc, "applies_department": department, "review": {}}
    if not can_action(user, "document.review_department", bindings, scoped_doc):
        raise PermissionError("Không được duyệt thay phòng ban khác.")
    for uid in selected:
        current = user_record(conn, uid)
        if (
            not current
            or current["company_id"] != doc["applies_company"]
            or current["department_id"] != department
        ):
            raise ValueError("Nhân sự đã đổi đơn vị. Cần giám đốc duyệt lại phạm vi.")
    conn.execute(
        "UPDATE department_reviews SET selected_json=?,status='approved',approved_by=? WHERE document_id=? AND department_id=?",
        (json.dumps(sorted(set(selected))), user["id"], doc["id"], department),
    )
    conn.execute(
        "UPDATE document_reviews SET revision=revision+1,updated_at=? WHERE document_id=?",
        (now(), doc["id"]),
    )
    finish_if_ready(conn, doc["id"])
    return {
        "department_id": department,
        "selected_user_ids": sorted(set(selected)),
        "stage": review_record(conn, doc["id"])["stage"],
    }


def finish_if_ready(conn, document_id):
    review = review_record(conn, document_id)
    if review["stage"] != "departments" or any(
        r["status"] != "approved" for r in review["departments"]
    ):
        return
    selected = sorted(
        set(review["direct_users"]) | {uid for r in review["departments"] for uid in r["selected"]}
    )
    # Use individually approved principals: a company-wide alias would expose omitted people.
    conn.execute(
        "UPDATE documents SET allow_json=?,deny_json=?,approval='approved' WHERE id=?",
        (
            json.dumps(["user:" + uid for uid in selected]),
            json.dumps(review["proposed_deny"]),
            document_id,
        ),
    )
    conn.execute(
        "UPDATE document_reviews SET stage='approved',updated_at=? WHERE document_id=?",
        (now(), document_id),
    )
    conn.execute("UPDATE relations SET approved=1 WHERE source_id=?", (document_id,))


def approve_override(conn, doc, user, payload):
    review = review_record(conn, doc["id"])
    check_revision(review, payload)
    if doc["status"] != "staging" or review["stage"] == "approved":
        raise ValueError("Chỉ duyệt trực tiếp bản nháp chưa hoàn tất.")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ValueError("Cần ghi lý do chủ tịch duyệt trực tiếp để lưu audit.")
    allow = payload.get("allow", review["proposed_allow"])
    deny = payload.get("deny", review["proposed_deny"])
    people = resolve_viewers(conn, doc, allow, deny)
    from .importer import validate_personal_acl

    validate_personal_acl(conn, doc, allow, user)
    if not people:
        raise ValueError("Cần người xem trong phạm vi tài liệu.")
    conn.execute(
        "UPDATE documents SET allow_json=?,deny_json=?,approval='approved' WHERE id=?",
        (json.dumps(["user:" + u["id"] for u in people]), json.dumps(deny), doc["id"]),
    )
    conn.execute("DELETE FROM department_reviews WHERE document_id=?", (doc["id"],))
    conn.execute(
        "UPDATE document_reviews SET stage='approved',revision=revision+1,company_approved_by=?,updated_at=?,override_reason=?,company_candidates_json=? WHERE document_id=?",
        (user["id"], now(), reason.strip(), json.dumps([u["id"] for u in people]), doc["id"]),
    )
    return {
        "reason": reason.strip(),
        "selected_user_ids": [u["id"] for u in people],
        "override": True,
    }


def validate_publication(conn, doc):
    review = review_record(conn, doc["id"])
    if not review or review["stage"] != "approved":
        raise ValueError("Chưa hoàn tất chuỗi duyệt người xem.")
    resolve_viewers(conn, doc, doc["allow"], doc["deny"])
    if review.get("override_reason"):
        return
    from .store import user_record

    for department in review["departments"]:
        if department["status"] != "approved":
            raise ValueError("Còn phòng ban chưa duyệt.")
        for uid in department["selected"]:
            current = user_record(conn, uid)
            if (
                not current
                or current["company_id"] != doc["applies_company"]
                or current["department_id"] != department["department_id"]
            ):
                raise ValueError("Nhân sự đã đổi phòng ban sau duyệt; cần duyệt lại danh sách.")
