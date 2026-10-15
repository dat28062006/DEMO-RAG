"""Import explicit demo documents as pending drafts. Text is data, never instructions."""

import hashlib
import json
import re
from datetime import date, datetime, timezone

from .store import (
    bindings_for,
    can_action,
    can_create_somewhere,
    can_create_group,
    connect,
    user_record,
    valid_read_principals,
    roles_for,
    validate_date,
)

LABELS = {
    "entitlement": "Số ngày được nghỉ",
    "usage": "Số ngày đã nghỉ",
    "balance": "Số ngày còn lại",
}


def leave_value(text, kind):
    pattern = r"^" + re.escape(LABELS[kind]) + r":\s*(\d+(?:[.,]\d+)?)\s+ngày(?:/năm)?[^\n]*$"
    matches = list(re.finditer(pattern, text, re.MULTILINE))
    if len(matches) != 1:
        raise ValueError("Cần đúng một dòng số ngày theo mẫu, không được suy ra từ nội dung mơ hồ.")
    amount = float(matches[0][1].replace(",", "."))
    if not 0 <= amount <= 366:
        raise ValueError("Số ngày trong mẫu phải từ 0 đến 366.")
    return int(amount) if amount.is_integer() else amount, matches[0][0]


def import_documents(db_path, payload, upload=None):
    bundle = payload.get("bundle")
    if not isinstance(bundle, dict) or bundle.get("format") != "mission2-documents-v1":
        raise ValueError("Gói JSON cần đúng định dạng mission2-documents-v1.")
    documents = bundle.get("documents")
    if not isinstance(documents, list) or not 1 <= len(documents) <= 20:
        raise ValueError("Mỗi gói cần 1 đến 20 tài liệu.")
    ids = []
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = user_record(conn, payload.get("user_id"))
        bindings = bindings_for(conn)
        if not user or not can_create_somewhere(user, bindings):
            raise PermissionError("Chưa được cấp quyền tạo tài liệu trong phạm vi này.")
        groups = {
            g for row in conn.execute("SELECT groups_json FROM users") for g in json.loads(row[0])
        }
        users = {row[0] for row in conn.execute("SELECT id FROM users")}
        orgs = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM organizations")}
        for source in documents:
            if not isinstance(source, dict):
                raise ValueError("Tài liệu phải là object JSON.")
            strings = ("id", "title", "section", "version", "text", "effective_from")
            if any(
                not isinstance(source.get(key), str) or not source[key].strip() for key in strings
            ):
                raise ValueError("Thiếu mã, tên, phiên bản, mục, nội dung hoặc ngày hiệu lực.")
            sid = source["id"]
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid)
                or sid in ids
                or conn.execute("SELECT 1 FROM documents WHERE id=?", (sid,)).fetchone()
            ):
                raise ValueError(
                    "Mã tài liệu không hợp lệ hoặc đã tồn tại. Import không ghi đè nguồn cũ."
                )
            if (
                len(source["text"]) > 60000
                or len(source["title"]) > 200
                or len(source["section"]) > 120
                or len(source["version"]) > 32
            ):
                raise ValueError("Nội dung hoặc metadata quá dài.")
            validate_date(source["effective_from"])
            end = source.get("effective_to")
            if end is not None and (
                not isinstance(end, str)
                or validate_date(end) < validate_date(source["effective_from"])
            ):
                raise ValueError("Ngày hết hiệu lực không hợp lệ.")
            company, department = source.get("applies_company"), source.get("applies_department")
            role = source.get("applies_role")
            group_scope = source.get("scope_level") == "group"
            if group_scope:
                if company is not None or department is not None:
                    raise ValueError("Tài liệu cấp tập đoàn không được gắn một công ty hoặc phòng riêng.")
                if not can_create_group(conn, user, bindings):
                    raise PermissionError("Chưa được cấp quyền nhập tài liệu dùng chung cấp tập đoàn.")
            elif (
                not isinstance(company, str)
                or company not in orgs
                or orgs[company]["level"] != "company"
                or (
                    department is not None
                    and (
                        not isinstance(department, str)
                        or department not in orgs
                        or orgs[department]["parent_id"] != company
                    )
                )
            ):
                raise ValueError("Công ty hoặc phòng ban không hợp lệ.")
            if role is not None and (not isinstance(role, str) or role not in roles_for(conn)):
                raise ValueError("Vai trò áp dụng không hợp lệ.")
            allow, deny = source.get("allow"), source.get("deny", [])
            valid = valid_read_principals(conn)
            if (
                not isinstance(allow, list)
                or not allow
                or not isinstance(deny, list)
                or any(not isinstance(v, str) or v not in valid for v in allow + deny)
            ):
                raise ValueError("Nhóm hoặc người được đọc không hợp lệ.")
            draft = {
                "applies_company": company,
                "applies_department": department,
                "allow": allow,
                "deny": deny,
                "status": "staging",
                "owner_id": user["id"],
            }
            if not can_action(user, "document.create_version", bindings, draft):
                raise PermissionError(
                    "Chưa được cấp quyền tạo tài liệu trong phạm vi này hoặc ACL không cho phép tác giả quản lý."
                )
            if source.get("policy_key") not in ("leave", "meal", "remote", "bonus", "general"):
                raise ValueError("Chủ đề tài liệu không hợp lệ.")
            facts = source.get("facts")
            if source["policy_key"] == "leave" and (
                not isinstance(facts, dict)
                or not isinstance(facts.get("kind"), str)
                or facts["kind"] not in LABELS
                or facts.get("leave_type") != "nghỉ phép năm"
            ):
                raise ValueError("Cần loại nguồn entitlement/usage/balance và loại nghỉ phép năm.")
            if source["policy_key"] != "leave" and facts is not None:
                raise ValueError("Chỉ nguồn nghỉ phép được gắn metadata cá nhân.")
            facts = (
                {
                    key: facts.get(key)
                    for key in ("kind", "leave_type", "subject_id", "period_start", "period_end")
                }
                if facts
                else None
            )
            if facts and facts["kind"] in ("usage", "balance"):
                person = (
                    user_record(conn, facts["subject_id"])
                    if isinstance(facts["subject_id"], str)
                    else None
                )
                if (
                    not person
                    or person["company_id"] != company
                    or (department and person["department_id"] != department)
                ):
                    raise ValueError("Hồ sơ cá nhân không khớp phạm vi của tài liệu.")
                for key in ("period_start", "period_end"):
                    if not isinstance(facts[key], str):
                        raise ValueError("Nguồn cá nhân cần kỳ dữ liệu.")
                    validate_date(facts[key])
                if facts["period_start"] > facts["period_end"]:
                    raise ValueError("Kỳ dữ liệu bị đảo ngược.")
                # Personal records cannot be exposed to a company-wide or public group.
                hr_departments = {
                    row["department_id"]
                    for row in conn.execute(
                        "SELECT department_id FROM users WHERE role='hr' AND company_id=?",
                        (company,),
                    )
                    if row["department_id"]
                }
                hr_members = {
                    "user:" + row["id"]
                    for row in conn.execute("SELECT id,department_id FROM users")
                    if row["department_id"] in hr_departments
                }
                personal_readers = (
                    {"user:" + facts["subject_id"], "user:" + user["id"]}
                    | hr_members
                    | hr_departments
                    | {"department:" + dep for dep in hr_departments}
                )
                if any(v not in personal_readers for v in allow):
                    raise ValueError(
                        "Nguồn lịch sử/số dư cá nhân chỉ cấp cho chủ hồ sơ, tác giả hoặc HR trong công ty; không chia sẻ cả công ty/phòng nghiệp vụ."
                    )
            elif facts and any(
                facts[key] is not None for key in ("subject_id", "period_start", "period_end")
            ):
                raise ValueError("Chính sách chung không được gắn số dư hoặc kỳ cá nhân.")
            text = source["text"].strip()
            marker = "## " + source["section"] + "\n"
            if marker not in text:
                raise ValueError("Nội dung thiếu tiêu đề mục khớp metadata.")
            section = (
                text.split(marker, 1)[1].strip()
                if upload
                else text.split(marker, 1)[1].split("\n## ", 1)[0].strip()
            )
            if facts:
                leave_value(section, facts["kind"])
            checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    source["title"],
                    source["version"],
                    source["section"],
                    source["policy_key"],
                    source["effective_from"],
                    end,
                    company,
                    department,
                    role,
                    1,
                    json.dumps(allow),
                    json.dumps(deny),
                    "staging",
                    text,
                    section,
                    checksum,
                    "pending",
                ),
            )
            if facts:
                conn.execute(
                    "INSERT INTO document_facts VALUES(?,?)",
                    (sid, json.dumps(facts, ensure_ascii=False)),
                )
            if upload:
                conn.execute(
                    "INSERT INTO document_uploads VALUES(?,?,?,?,?,?)",
                    (
                        sid,
                        upload["filename"],
                        upload["media_type"],
                        upload["file_checksum"],
                        upload["original"],
                        json.dumps(upload["extraction"], ensure_ascii=False),
                    ),
                )
            conn.execute(
                "INSERT INTO audit(actor_id,document_id,action,occurred_at,detail_json) VALUES(?,?,?,?,?)",
                (
                    user["id"],
                    sid,
                    "document.import",
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps({"checksum": checksum, "status": "staging", "approval": "pending"}),
                ),
            )
            ids.append(sid)
    from .hybrid import index_document
    from .store import document_records

    with connect(db_path) as conn:
        saved = [d for d in document_records(conn) if d["id"] in ids]
    from .review_workflow import ensure_review

    with connect(db_path) as conn:
        for doc in saved:
            ensure_review(conn, doc)
    indexes = {d["id"]: index_document(db_path, d) for d in saved}
    return {
        "saved": True,
        "document_ids": ids,
        "count": len(ids),
        "status": "staging",
        "approval": "pending",
        "indexes": indexes,
    }


def validate_personal_acl(conn, doc, allow, actor):
    """Viewer review must not turn a personal HR record into company-wide knowledge."""
    facts = doc.get("facts") or {}
    if facts.get("kind") not in ("usage", "balance"):
        return
    subject = facts.get("subject_id")
    departments = {
        r[0]
        for r in conn.execute(
            "SELECT department_id FROM users WHERE role='hr' AND company_id=?",
            (doc["applies_company"],),
        )
        if r[0]
    }
    permitted = {"user:" + subject}
    if doc.get("owner_id"):
        permitted.add("user:" + doc["owner_id"])
    permitted.update("department:" + dep for dep in departments)
    permitted.update(departments)
    permitted.update(
        "user:" + r[0]
        for r in conn.execute(
            "SELECT id,department_id FROM users WHERE company_id=?", (doc["applies_company"],)
        )
        if r[1] in departments
    )
    if any(principal not in permitted for principal in allow):
        raise ValueError("Hồ sơ cá nhân không được cấp toàn công ty hoặc người ngoài chủ hồ sơ/HR.")
