"""Document actions and diagnostic reports authorized by explicit action bindings."""

import hashlib
import json
import uuid
from datetime import date, datetime, timezone

from .store import (
    access_tree,
    bindings_for,
    can_action,
    can_create_somewhere,
    can_read,
    connect,
    document_records,
    user_record,
    valid_read_principals,
    today_local,
    validity_info,
    validate_date,
)
from .hybrid import config_for, config_hash, index_document, manifest_for

MANAGEMENT_ACTIONS = (
    "document.catalog",
    "document.approve",
    "document.review_company",
    "document.review_department",
    "document.override_review",
    "document.create_version",
    "document.publish",
    "document.withdraw",
    "acl.manage",
    "audit.read",
)
GROUP_LABELS = {
    "PUBLIC": "Toàn bộ hồ sơ demo",
    "A_STAFF": "Nhân viên công ty A",
    "B_STAFF": "Nhân viên công ty B",
    "A_OPS": "Vận hành A",
    "A_HR": "HR A",
    "A_LEADERS": "Lãnh đạo A",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def context(conn, user_id):
    user = user_record(conn, user_id)
    if not user:
        raise PermissionError("Không có quyền thực hiện thao tác này.")
    return user, bindings_for(conn)


def doc_permissions(user, bindings, doc):
    return [action for action in MANAGEMENT_ACTIONS if can_action(user, action, bindings, doc)]


def managed_doc(conn, user, bindings, doc_id):
    doc = next((d for d in document_records(conn) if d["id"] == doc_id), None)
    if not doc or not doc_permissions(user, bindings, doc):
        raise PermissionError("Không có quyền quản lý tài liệu này.")
    return doc


def summary(doc, permissions):
    keys = (
        "id",
        "title",
        "version",
        "section",
        "checksum",
        "effective_from",
        "effective_to",
        "applies_company",
        "applies_department",
        "status",
        "approval",
        "allow",
        "deny",
    )
    return {
        **{key: doc[key] for key in keys},
        "permissions": permissions,
        "review": doc.get("review"),
    }


class ControlService:
    def __init__(self, db_path):
        self.db_path = db_path

    def catalogue(self, user_id):
        with connect(self.db_path) as conn:
            user, bindings = context(conn, user_id)
            documents = []
            docs = document_records(conn)
            relations = [dict(r) for r in conn.execute("SELECT * FROM relations WHERE approved=1")]
            for doc in docs:
                permissions = doc_permissions(user, bindings, doc)
                if permissions:
                    documents.append(
                        {
                            **summary(doc, permissions),
                            **validity_info(doc, today_local(), user, docs, relations),
                            "index": manifest_for(self.db_path, doc["id"]),
                        }
                    )
            if (
                not documents
                and not can_create_somewhere(user, bindings)
                and not any(can_action(user, action, bindings) for action in MANAGEMENT_ACTIONS)
            ):
                raise PermissionError("Không có quyền truy cập khu vực quản lý tài liệu.")
            principals = {
                **GROUP_LABELS,
                **{
                    "user:" + row["id"]: "Cá nhân " + row["name"]
                    for row in conn.execute("SELECT id,name FROM users")
                },
            }
            return {"documents": documents, "groups": principals, "access_tree": access_tree(conn)}

    def detail(self, user_id, doc_id):
        with connect(self.db_path) as conn:
            user, bindings = context(conn, user_id)
            doc = managed_doc(conn, user, bindings, doc_id)
            output = summary(doc, doc_permissions(user, bindings, doc))
            docs = document_records(conn)
            relations = [dict(r) for r in conn.execute("SELECT * FROM relations WHERE approved=1")]
            output.update(validity_info(doc, today_local(), user, docs, relations))
            output["text"] = doc["source_text"]
            output["section_text"] = doc["section_text"]
            output["index"] = manifest_for(self.db_path, doc_id)
            output["review_actions"] = (
                {
                    "company": can_action(user, "document.review_company", bindings, doc),
                    "override": can_action(user, "document.override_review", bindings, doc),
                    "departments": [
                        r["department_id"]
                        for r in doc.get("review", {}).get("departments", [])
                        if can_action(
                            user,
                            "document.review_department",
                            bindings,
                            {**doc, "applies_department": r["department_id"], "review": {}},
                        )
                    ],
                }
                if doc.get("review")
                else None
            )
            if "origin" in doc:
                output["origin"] = {
                    key: doc["origin"].get(key)
                    for key in (
                        "filename",
                        "media_type",
                        "file_checksum",
                        "extractor",
                        "page_count",
                    )
                }
            output["audit"] = []
            if can_action(user, "audit.read", bindings, doc):
                output["audit"] = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT actor_id,action,occurred_at FROM audit WHERE document_id=? ORDER BY id DESC LIMIT 20",
                        (doc_id,),
                    )
                ]
            return output

    def update(self, doc_id, payload):
        user_id, action = payload.get("user_id"), payload.get("action")
        mapping = {
            "approve": "document.approve",
            "publish": "document.publish",
            "withdraw": "document.withdraw",
            "set_permissions": "acl.manage",
            "create_version": "document.create_version",
            "reindex": "document.create_version",
            "approve_company": "document.review_company",
            "approve_department": "document.review_department",
            "approve_override": "document.override_review",
        }
        if action not in mapping:
            raise ValueError("Thao tác không hợp lệ.")
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            user, bindings = context(conn, user_id)
            doc = managed_doc(conn, user, bindings, doc_id)
            if not can_action(user, mapping[action], bindings, doc):
                raise PermissionError(
                    "Chưa được cấp quyền cho thao tác này trong phạm vi tài liệu."
                )
            if (
                action
                in (
                    "approve",
                    "approve_company",
                    "approve_department",
                    "approve_override",
                    "publish",
                    "withdraw",
                    "create_version",
                    "set_permissions",
                )
                and payload.get("expected_checksum") is not None
                and payload["expected_checksum"] != doc["checksum"]
            ):
                raise ValueError(
                    "Nội dung tài liệu đã thay đổi. Hãy tải lại để xem đúng phiên bản trước khi thao tác."
                )
            detail = {}
            result_id = doc_id
            from .review_workflow import (
                ensure_review,
                enabled,
                review_record,
                approve_company,
                approve_department,
                approve_override,
            )

            if (
                action
                in (
                    "approve",
                    "approve_company",
                    "approve_department",
                    "approve_override",
                    "publish",
                )
                and doc["status"] == "staging"
                and enabled(conn, doc)
            ):
                ensure_review(conn, doc)
                doc["review"] = review_record(conn, doc_id)
            if action in ("approve_company", "approve_department", "approve_override"):
                if not doc.get("review"):
                    raise ValueError("Tài liệu chưa được cấu hình luồng duyệt phân cấp.")
                if action == "approve_company":
                    detail = approve_company(conn, doc, user, payload)
                elif action == "approve_department":
                    detail = approve_department(conn, doc, user, bindings, payload)
                else:
                    detail = approve_override(conn, doc, user, payload)
                    conn.execute("UPDATE relations SET approved=1 WHERE source_id=?", (doc_id,))
            if action == "approve":
                if doc.get("review"):
                    raise ValueError(
                        "Cần duyệt theo chuỗi giám đốc → trưởng phòng hoặc dùng quyền duyệt trực tiếp cấp tập đoàn."
                    )
                if doc["status"] != "staging" or doc["approval"] != "pending":
                    raise ValueError(
                        "Chỉ duyệt bản nháp đang chờ duyệt; bản công bố/thu hồi phải tạo phiên bản mới."
                    )
                conn.execute("UPDATE documents SET approval='approved' WHERE id=?", (doc_id,))
                conn.execute("UPDATE relations SET approved=1 WHERE source_id=?", (doc_id,))
            elif action == "publish":
                if doc.get("review") and doc["review"]["stage"] != "approved":
                    raise ValueError(
                        "Chưa hoàn tất chuỗi duyệt người xem của công ty và các phòng ban."
                    )
                if doc.get("review"):
                    from .review_workflow import validate_publication

                    validate_publication(conn, doc)
                if doc["status"] != "staging" or doc["approval"] != "approved":
                    raise ValueError(
                        "Tài liệu chưa được duyệt. Duyệt và công bố là hai hành động riêng."
                    )
                if (
                    doc["effective_to"]
                    and doc["effective_to"] < today_local()
                    and payload.get("archive") is not True
                ):
                    raise ValueError(
                        "Tài liệu đã hết hiệu lực. Chỉ công bố bản lịch sử, không dùng làm nguồn hiện hành."
                    )
                config = config_for(self.db_path)
                if config and config.get("enabled"):
                    index = manifest_for(self.db_path, doc_id)
                    if (
                        index["status"] != "ready"
                        or index.get("source_checksum") != doc["checksum"]
                        or index.get("config_hash") != config_hash(config)
                    ):
                        raise ValueError(
                            "Chỉ mục chưa sẵn sàng hoặc sai revision. Hãy lập lại chỉ mục trước khi công bố."
                        )
                detail = {
                    "historical_only": bool(
                        doc["effective_to"] and doc["effective_to"] < today_local()
                    )
                }
                conn.execute("UPDATE documents SET status='published' WHERE id=?", (doc_id,))
            elif action == "withdraw":
                if doc["status"] != "published":
                    raise ValueError("Chỉ thu hồi tài liệu đã công bố.")
                conn.execute("UPDATE documents SET status='withdrawn' WHERE id=?", (doc_id,))
            elif action == "set_permissions":
                if doc.get("review"):
                    from .review_workflow import check_revision

                    check_revision(doc["review"], payload)
                allow, deny = payload.get("allow"), payload.get("deny")
                valid = valid_read_principals(conn)
                if (
                    not isinstance(allow, list)
                    or not isinstance(deny, list)
                    or any(not isinstance(v, str) or v not in valid for v in allow + deny)
                ):
                    raise ValueError("Chỉ chấp nhận các nhóm được khai báo trong cấu hình demo.")
                detail = {
                    "before": {"allow": doc["allow"], "deny": doc["deny"]},
                    "after": {"allow": allow, "deny": deny},
                }
                conn.execute(
                    "UPDATE documents SET allow_json=?,deny_json=? WHERE id=?",
                    (json.dumps(sorted(set(allow))), json.dumps(sorted(set(deny))), doc_id),
                )
                if enabled(conn, doc):
                    from .importer import validate_personal_acl
                    from .review_workflow import resolve_viewers

                    resolve_viewers(conn, doc, allow, deny)
                    validate_personal_acl(conn, doc, allow, user)
                    if can_action(user, "document.override_review", bindings, doc):
                        current_review = review_record(conn, doc_id)
                        if current_review and doc["status"] == "published":
                            conn.execute(
                                "DELETE FROM department_reviews WHERE document_id=?", (doc_id,)
                            )
                            conn.execute(
                                "UPDATE document_reviews SET revision=revision+1,proposed_allow_json=?,proposed_deny_json=?,updated_at=?,override_reason='Cập nhật quyền bởi quản trị tập đoàn' WHERE document_id=?",
                                (json.dumps(allow), json.dumps(deny), now(), doc_id),
                            )
                        else:
                            ensure_review(conn, doc, force=True, allow=allow, deny=deny)
                            detail["review_restarted"] = True
                    else:
                        ensure_review(conn, doc, force=True, allow=allow, deny=deny)
                        detail["review_restarted"] = True
            elif action == "create_version":
                body = payload.get("body")
                effective = payload.get("effective_from")
                end = payload.get("effective_to") or None
                title = payload.get("title", doc["title"])
                if (
                    not isinstance(body, str)
                    or not body.strip()
                    or len(body) > 58000
                    or not isinstance(effective, str)
                    or not isinstance(title, str)
                    or not 1 <= len(title.strip()) <= 200
                ):
                    raise ValueError("Cần nội dung mới và ngày hiệu lực hợp lệ.")
                validate_date(effective)
                if end is not None and (
                    not isinstance(end, str) or validate_date(end) < validate_date(effective)
                ):
                    raise ValueError("Ngày hết hiệu lực không được trước ngày bắt đầu.")
                result_id = doc_id + "-V" + uuid.uuid4().hex[:6]
                version = (
                    str(int(doc["version"]) + 1)
                    if doc["version"].isdigit()
                    else doc["version"] + "-next"
                )
                title = title.strip()
                text = f"# {title} — phiên bản {version}\n\n## {doc['section']}\n{body.strip()}"
                checksum = hashlib.sha256(text.encode()).hexdigest()
                conn.execute(
                    """INSERT INTO documents
                  (id,title,version,section,policy_key,effective_from,effective_to,applies_company,applies_department,applies_role,
                   required,allow_json,deny_json,status,source_text,section_text,checksum,approval)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        result_id,
                        title,
                        version,
                        doc["section"],
                        doc["policy_key"],
                        effective,
                        end,
                        doc["applies_company"],
                        doc["applies_department"],
                        doc["applies_role"],
                        doc["required"],
                        json.dumps(doc["allow"]),
                        json.dumps(doc["deny"]),
                        "staging",
                        text,
                        body.strip(),
                        checksum,
                        "pending",
                    ),
                )
                conn.execute(
                    "INSERT INTO relations VALUES(?,?,'supersedes',0)", (result_id, doc_id)
                )
                # A new version is the same policy: retain the parent's explicit override
                # targets. These links still require approval and a published active source.
                inherited = [
                    row["target_id"]
                    for row in conn.execute(
                        "SELECT target_id FROM relations WHERE source_id=? AND type='overrides' AND approved=1",
                        (doc_id,),
                    )
                ]
                for target in inherited:
                    conn.execute(
                        "INSERT INTO relations VALUES(?,?,'overrides',0)", (result_id, target)
                    )
                if "facts" in doc:
                    from .importer import leave_value

                    leave_value(body.strip(), doc["facts"]["kind"])
                    conn.execute(
                        "INSERT INTO document_facts VALUES(?,?)",
                        (result_id, json.dumps(doc["facts"], ensure_ascii=False)),
                    )
                detail = {
                    "parent_id": doc_id,
                    "new_id": result_id,
                    "version": version,
                    "effective_from": effective,
                    "effective_to": end,
                    "before_checksum": doc["checksum"],
                    "after_checksum": checksum,
                    "inherited_override_targets": inherited,
                }
                conn.execute(
                    "INSERT INTO audit(actor_id,document_id,action,occurred_at,detail_json) VALUES(?,?,?,?,?)",
                    (
                        user_id,
                        result_id,
                        "document.create_version",
                        now(),
                        json.dumps(detail, ensure_ascii=False),
                    ),
                )
            conn.execute(
                "INSERT INTO audit(actor_id,document_id,action,occurred_at,detail_json) VALUES(?,?,?,?,?)",
                (user_id, doc_id, mapping[action], now(), json.dumps(detail, ensure_ascii=False)),
            )
        output = {"saved": True, "document_id": result_id}
        if action == "create_version":
            with connect(self.db_path) as conn:
                created = next(d for d in document_records(conn) if d["id"] == result_id)
                ensure_review(conn, created)
        if action in ("create_version", "reindex"):
            with connect(self.db_path) as conn:
                indexed = next(d for d in document_records(conn) if d["id"] == result_id)
            output["index"] = index_document(self.db_path, indexed)
        return output

    def submit_feedback(self, payload):
        uid, rid = payload.get("user_id"), payload.get("request_id")
        description = payload.get("description", "")
        category = payload.get("category", "wrong_answer")
        request_ids = payload.get("conversation_request_ids", [rid])
        if not isinstance(description, str) or not description.strip() or len(description) > 4000:
            raise ValueError("Hãy mô tả lỗi, tối đa 4.000 ký tự.")
        if (
            category not in ("wrong_answer", "missing_source", "permission", "other")
            or not isinstance(request_ids, list)
            or len(request_ids) > 100
        ):
            raise ValueError("Report không hợp lệ.")
        if not isinstance(rid, str) or not all(isinstance(item, str) for item in request_ids):
            raise ValueError("Cần request ID hợp lệ.")
        with connect(self.db_path) as conn:
            user, bindings = context(conn, uid)
            if not can_action(user, "feedback.submit", bindings):
                raise PermissionError("Chưa được cấp quyền báo lỗi.")
            window = []
            ids = list(dict.fromkeys(request_ids + [rid]))
            for request_id in ids:
                row = conn.execute(
                    "SELECT rowid AS sequence,* FROM query_runs WHERE request_id=? AND user_id=?",
                    (request_id, uid),
                ).fetchone()
                if not row:
                    raise PermissionError(
                        "Không thể gắn context của một bối cảnh khác vào report này."
                    )
                window.append(
                    {
                        "request_id": row["request_id"],
                        "sequence": row["sequence"],
                        "created_at": row["created_at"],
                        "response": json.loads(row["response_json"]),
                        "context": json.loads(row["context_json"]),
                    }
                )
            window.sort(key=lambda turn: turn["sequence"])
            snapshot = {
                "reported_request_id": rid,
                "conversation": window,
                "reported_by": uid,
                "captured_at": now(),
                "runtime": (
                    "hybrid RAG; prompt, evidence, output và revision LLM được lưu cho lượt dùng model"
                    if any(t["context"].get("generation") for t in window)
                    else "tra cứu/trích xuất; lượt này không gọi LLM"
                ),
                "client_claims_ignored": True,
            }
            feedback_id = "RPT-" + uuid.uuid4().hex[:10]
            conn.execute(
                "INSERT INTO feedback VALUES(?,?,?,?,?,?,?,?)",
                (
                    feedback_id,
                    uid,
                    rid,
                    now(),
                    category,
                    description.strip(),
                    "open",
                    json.dumps(snapshot, ensure_ascii=False),
                ),
            )
        return {"saved": True, "id": feedback_id, "turns_saved": len(window)}

    def feedback_list(self, user_id):
        with connect(self.db_path) as conn:
            user, bindings = context(conn, user_id)
            if not can_action(user, "feedback.review", bindings):
                raise PermissionError("Chưa được cấp quyền xem report của nhà phát triển.")
            docs = {doc["id"]: doc for doc in document_records(conn)}
            output = []
            for row in conn.execute("SELECT * FROM feedback ORDER BY created_at DESC LIMIT 200"):
                snapshot = json.loads(row["snapshot_json"])
                source_ids = {
                    source["id"]
                    for turn in snapshot["conversation"]
                    for source in turn["context"].get("sources", [])
                }
                visible = all(
                    doc_id in docs
                    and (
                        can_read(user, docs[doc_id])
                        or can_action(user, "document.catalog", bindings, docs[doc_id])
                    )
                    for doc_id in source_ids
                )
                item = {key: row[key] for key in ("id", "created_at", "category", "status")}
                item["restricted"] = not visible
                if visible:
                    item.update(
                        actor_id=row["actor_id"],
                        request_id=row["request_id"],
                        description=row["description"],
                        snapshot=snapshot,
                    )
                output.append(item)
            return {"reports": output}

    def feedback_status(self, feedback_id, payload):
        if payload.get("status") not in ("open", "investigating", "resolved"):
            raise ValueError("Trạng thái report không hợp lệ.")
        with connect(self.db_path) as conn:
            user, bindings = context(conn, payload.get("user_id"))
            if not can_action(user, "feedback.review", bindings):
                raise PermissionError("Chưa được cấp quyền xử lý report.")
            if not conn.execute("SELECT 1 FROM feedback WHERE id=?", (feedback_id,)).fetchone():
                raise ValueError("Report không tồn tại.")
            conn.execute(
                "UPDATE feedback SET status=? WHERE id=?", (payload["status"], feedback_id)
            )
        return {"saved": True}
