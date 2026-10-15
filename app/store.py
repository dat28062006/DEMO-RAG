"""SQLite source of truth. Uses only Python standard-library dependencies."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "runtime" / "mission2-v2.sqlite3"
SEED_FILE = ROOT / "data" / "seed.json"
DEFAULT_ROLES = {
    "employee": "Nhân viên",
    "hr": "HR / nghiệp vụ",
    "leader": "Chủ tài liệu / người công bố",
    "it_security": "IT / an ninh",
    "chairman": "Chủ tịch tập đoàn",
    "company_director": "Giám đốc công ty",
    "department_head": "Trưởng phòng",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS roles (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS organizations (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, parent_id TEXT REFERENCES organizations(id), level TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, name TEXT NOT NULL,
 company_id TEXT REFERENCES organizations(id), department_id TEXT REFERENCES organizations(id),
 role TEXT NOT NULL, groups_json TEXT NOT NULL, description TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, version TEXT NOT NULL, section TEXT NOT NULL,
 policy_key TEXT NOT NULL, effective_from TEXT NOT NULL, effective_to TEXT,
 applies_company TEXT REFERENCES organizations(id), applies_department TEXT REFERENCES organizations(id),
 applies_role TEXT, required INTEGER NOT NULL CHECK(required IN (0,1)),
 allow_json TEXT NOT NULL, deny_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('published','staging','withdrawn')),
 source_text TEXT NOT NULL, section_text TEXT NOT NULL, checksum TEXT NOT NULL,
 approval TEXT NOT NULL DEFAULT 'approved' CHECK(approval IN ('pending','approved','rejected')),
 CHECK(effective_to IS NULL OR effective_to >= effective_from), UNIQUE(id,version)
);
CREATE TABLE IF NOT EXISTS relations (
 source_id TEXT NOT NULL REFERENCES documents(id), target_id TEXT NOT NULL REFERENCES documents(id),
 type TEXT NOT NULL CHECK(type IN ('supersedes','overrides')), approved INTEGER NOT NULL CHECK(approved IN (0,1)),
 PRIMARY KEY(source_id,target_id,type), CHECK(source_id != target_id)
);
CREATE TABLE IF NOT EXISTS document_facts (
 document_id TEXT PRIMARY KEY REFERENCES documents(id), metadata_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_uploads (
 document_id TEXT PRIMARY KEY REFERENCES documents(id), filename TEXT NOT NULL,
 media_type TEXT NOT NULL, file_checksum TEXT NOT NULL, original BLOB NOT NULL,
 extraction_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rag_indexes (
 document_id TEXT PRIMARY KEY REFERENCES documents(id), source_checksum TEXT NOT NULL,
 model TEXT NOT NULL, model_digest TEXT NOT NULL, dimensions INTEGER NOT NULL,
 config_hash TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('processing','ready','failed')),
 parent_count INTEGER NOT NULL DEFAULT 0, child_count INTEGER NOT NULL DEFAULT 0,
 error TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rag_parents (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id), source_checksum TEXT NOT NULL,
 start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, text TEXT NOT NULL,
 heading TEXT NOT NULL, line_start INTEGER NOT NULL, line_end INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rag_children (
 id TEXT PRIMARY KEY, parent_id TEXT NOT NULL REFERENCES rag_parents(id),
 document_id TEXT NOT NULL REFERENCES documents(id), start_offset INTEGER NOT NULL,
 end_offset INTEGER NOT NULL, text TEXT NOT NULL, terms_json TEXT NOT NULL, embedding BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS rag_children_doc ON rag_children(document_id);
CREATE INDEX IF NOT EXISTS rag_parents_doc ON rag_parents(document_id);
CREATE TABLE IF NOT EXISTS role_bindings (
 id INTEGER PRIMARY KEY, subject TEXT NOT NULL, action TEXT NOT NULL,
 effect TEXT NOT NULL CHECK(effect IN ('allow','deny')), scope_company TEXT REFERENCES organizations(id),
 include_descendants INTEGER NOT NULL DEFAULT 0 CHECK(include_descendants IN (0,1))
);
CREATE TABLE IF NOT EXISTS organization_read_bindings (
 id INTEGER PRIMARY KEY, subject TEXT NOT NULL,
 organization_id TEXT NOT NULL REFERENCES organizations(id),
 include_descendants INTEGER NOT NULL CHECK(include_descendants IN (0,1)),
 effect TEXT NOT NULL CHECK(effect IN ('allow','deny')),
 UNIQUE(subject,organization_id,effect)
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, actor_id TEXT NOT NULL, document_id TEXT NOT NULL,
 action TEXT NOT NULL, occurred_at TEXT NOT NULL, detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS query_runs (
 request_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL, response_json TEXT NOT NULL, context_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
 id TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES users(id), request_id TEXT NOT NULL REFERENCES query_runs(request_id),
 created_at TEXT NOT NULL, category TEXT NOT NULL, description TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('open','investigating','resolved')),
 snapshot_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS organization_review_policies (
 organization_id TEXT PRIMARY KEY REFERENCES organizations(id),
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1))
);
CREATE TABLE IF NOT EXISTS document_reviews (
 document_id TEXT PRIMARY KEY REFERENCES documents(id), revision INTEGER NOT NULL,
 stage TEXT NOT NULL CHECK(stage IN ('company','departments','approved')),
 proposed_allow_json TEXT NOT NULL, proposed_deny_json TEXT NOT NULL,
 company_candidates_json TEXT NOT NULL DEFAULT '[]', direct_users_json TEXT NOT NULL DEFAULT '[]',
 company_approved_by TEXT REFERENCES users(id), updated_at TEXT NOT NULL, override_reason TEXT
);
CREATE TABLE IF NOT EXISTS department_reviews (
 document_id TEXT NOT NULL REFERENCES document_reviews(document_id),
 department_id TEXT NOT NULL REFERENCES organizations(id),
 candidates_json TEXT NOT NULL, selected_json TEXT NOT NULL DEFAULT '[]',
 status TEXT NOT NULL CHECK(status IN ('pending','approved')),
 approved_by TEXT REFERENCES users(id), PRIMARY KEY(document_id,department_id)
);
"""


class ManagedConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10, factory=ManagedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _no_cycles(edges: dict[str, list[str]]) -> None:
    visiting, visited = set(), set()

    def visit(node):
        if node in visiting:
            raise ValueError("Cyclic organization or policy relation")
        if node in visited:
            return
        visiting.add(node)
        for target in edges.get(node, []):
            visit(target)
        visiting.remove(node)
        visited.add(node)

    for node in edges:
        visit(node)


def initialize(
    db_path: Path | str = DEFAULT_DB, reset: bool = False, seed_file: Path | str = SEED_FILE
) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = json.loads(Path(seed_file).read_text(encoding="utf-8"))
    _no_cycles({o["id"]: [o["parent_id"]] if o["parent_id"] else [] for o in seed["organizations"]})
    graph: dict[str, list[str]] = {}
    for relation in seed["relations"]:
        if relation["approved"]:
            graph.setdefault(relation["source_id"], []).append(relation["target_id"])
    _no_cycles(graph)
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        if "approval" not in {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}:
            conn.execute(
                "ALTER TABLE documents ADD COLUMN approval TEXT NOT NULL DEFAULT 'approved'"
            )
        if "include_descendants" not in {
            row["name"] for row in conn.execute("PRAGMA table_info(role_bindings)")
        }:
            conn.execute(
                "ALTER TABLE role_bindings ADD COLUMN include_descendants INTEGER NOT NULL DEFAULT 0"
            )
        for role_id, name in DEFAULT_ROLES.items():
            conn.execute("INSERT OR IGNORE INTO roles VALUES(?,?)", (role_id, name))
        if (
            conn.execute("SELECT 1 FROM settings WHERE key='dataset_revision'").fetchone()
            and not reset
        ):
            return
        if reset:
            for table in (
                "feedback",
                "query_runs",
                "audit",
                "department_reviews",
                "document_reviews",
                "organization_review_policies",
                "role_bindings",
                "organization_read_bindings",
                "relations",
                "rag_children",
                "rag_parents",
                "rag_indexes",
                "document_uploads",
                "document_facts",
                "documents",
                "users",
                "organizations",
                "roles",
                "settings",
            ):
                conn.execute(f"DELETE FROM {table}")
            for role_id, name in DEFAULT_ROLES.items():
                conn.execute("INSERT INTO roles VALUES(?,?)", (role_id, name))
        for role in seed.get("roles", []):
            conn.execute("INSERT OR IGNORE INTO roles VALUES(?,?)", (role["id"], role["name"]))
        for org in seed["organizations"]:
            conn.execute(
                "INSERT INTO organizations VALUES(?,?,?,?)",
                (org["id"], org["name"], org["parent_id"], org["level"]),
            )
        for user in seed["users"]:
            conn.execute(
                "INSERT INTO users VALUES(?,?,?,?,?,?,?)",
                (
                    user["id"],
                    user["name"],
                    user["company_id"],
                    user["department_id"],
                    user["role"],
                    json.dumps(user["groups"], ensure_ascii=False),
                    user["description"],
                ),
            )
        for binding in seed.get("role_bindings", []):
            conn.execute(
                "INSERT INTO role_bindings(subject,action,effect,scope_company,include_descendants) VALUES(?,?,?,?,?)",
                (
                    binding["subject"],
                    binding["action"],
                    binding["effect"],
                    binding["scope_company"],
                    int(binding.get("include_descendants", False)),
                ),
            )
        for binding in seed.get("organization_read_bindings", []):
            conn.execute(
                "INSERT INTO organization_read_bindings(subject,organization_id,include_descendants,effect) VALUES(?,?,?,?)",
                (
                    binding["subject"],
                    binding["organization_id"],
                    int(binding["include_descendants"]),
                    binding["effect"],
                ),
            )
        sources_root = (ROOT / "data" / "sources").resolve()
        for doc in seed["documents"]:
            date.fromisoformat(doc["effective_from"])
            if doc["effective_to"]:
                date.fromisoformat(doc["effective_to"])
            source = (sources_root / doc["source_path"]).resolve()
            if not source.is_relative_to(sources_root):
                raise ValueError("Source path outside fixture directory")
            text = source.read_text(encoding="utf-8").strip()
            marker = "## " + doc["section"] + "\n"
            if marker not in text or not text:
                raise ValueError("Source section missing")
            section = text.split(marker, 1)[1].split("\n## ", 1)[0].strip()
            checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
            conn.execute(
                """INSERT INTO documents VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    doc["id"],
                    doc["title"],
                    doc["version"],
                    doc["section"],
                    doc["policy_key"],
                    doc["effective_from"],
                    doc["effective_to"],
                    doc["applies_company"],
                    doc["applies_department"],
                    doc["applies_role"],
                    int(doc["required"]),
                    json.dumps(doc["allow"]),
                    json.dumps(doc["deny"]),
                    doc.get("status", "published"),
                    text,
                    section,
                    checksum,
                    doc.get("approval", "approved"),
                ),
            )
        for relation in seed["relations"]:
            conn.execute(
                "INSERT INTO relations VALUES(?,?,?,?)",
                (
                    relation["source_id"],
                    relation["target_id"],
                    relation["type"],
                    int(relation["approved"]),
                ),
            )
        for policy in seed.get("organization_review_policies", []):
            conn.execute(
                "INSERT INTO organization_review_policies VALUES(?,?)",
                (policy["organization_id"], int(policy["enabled"])),
            )
        conn.execute("INSERT INTO settings VALUES('dataset_revision',?)", (seed["revision"],))
        from .review_workflow import ensure_review

        for doc in document_records(conn):
            if doc["status"] == "staging":
                ensure_review(conn, doc)


def user_record(conn: sqlite3.Connection, user_id: str) -> dict | None:
    row = conn.execute(
        """SELECT u.*, c.name company_name, c.level organization_level, d.name department_name
        FROM users u LEFT JOIN organizations c ON c.id=u.company_id
        LEFT JOIN organizations d ON d.id=u.department_id WHERE u.id=?""",
        (user_id,),
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["groups"] = json.loads(result.pop("groups_json"))
    result["role_registered"] = bool(
        conn.execute("SELECT 1 FROM roles WHERE id=?", (result["role"],)).fetchone()
    )
    result["organization_read_scope"] = organization_read_scope(conn, result)
    subjects = binding_subjects(result)
    full_read = {"allow": set(), "deny": set()}
    for binding in bindings_for(conn):
        if binding["subject"] in subjects and binding["action"] == "document.read_all":
            ids = (
                binding["_scope_ids"]
                if binding["scope_company"]
                else [r[0] for r in conn.execute("SELECT id FROM organizations")]
            )
            full_read[binding["effect"]].update(ids)
    result["administrative_read_scope"] = sorted(full_read["allow"] - full_read["deny"])
    return result


def public_user(user: dict) -> dict:
    return {
        key: user[key]
        for key in (
            "id",
            "name",
            "company_id",
            "company_name",
            "department_id",
            "department_name",
            "role",
            "description",
            "organization_level",
        )
    }


def document_records(conn: sqlite3.Connection) -> list[dict]:
    result = []
    facts = {
        row["document_id"]: json.loads(row["metadata_json"])
        for row in conn.execute("SELECT * FROM document_facts")
    }
    uploads = {
        row["document_id"]: {
            "filename": row["filename"],
            "media_type": row["media_type"],
            "file_checksum": row["file_checksum"],
            **json.loads(row["extraction_json"]),
        }
        for row in conn.execute(
            "SELECT document_id,filename,media_type,file_checksum,extraction_json FROM document_uploads"
        )
    }
    owners = {
        row["document_id"]: row["actor_id"]
        for row in conn.execute(
            "SELECT document_id,actor_id,action,detail_json FROM audit WHERE action IN ('document.import','document.create_version') ORDER BY id"
        )
        if row["action"] == "document.import"
        or json.loads(row["detail_json"]).get("new_id") == row["document_id"]
    }
    for row in conn.execute("SELECT * FROM documents ORDER BY id"):
        doc = dict(row)
        doc["allow"] = json.loads(doc.pop("allow_json"))
        doc["deny"] = json.loads(doc.pop("deny_json"))
        if doc["id"] in facts:
            doc["facts"] = facts[doc["id"]]
        if doc["id"] in uploads:
            doc["origin"] = uploads[doc["id"]]
        if doc["id"] in owners:
            doc["owner_id"] = owners[doc["id"]]
        from .review_workflow import review_record

        review = review_record(conn, doc["id"])
        if review:
            doc["review"] = review
        result.append(doc)
    return result


def can_read(user: dict, doc: dict) -> bool:
    return (
        doc["status"] == "published"
        and doc.get("approval", "approved") == "approved"
        and (not doc.get("review") or doc["review"]["stage"] == "approved")
        and can_access(user, doc)
    )


def principals_for(user: dict) -> set[str]:
    principals = set(user["groups"]) | {"user:" + user["id"]}
    if user.get("company_id"):
        principals.add("company:" + user["company_id"])
    if user.get("department_id"):
        principals.add("department:" + user["department_id"])
    return principals


def can_access(user: dict, doc: dict) -> bool:
    principals = principals_for(user)
    scope = user.get("organization_read_scope", {})
    organization = (
        doc.get("applies_department") or doc.get("applies_company") or scope.get("group_root")
    )
    if principals.intersection(doc["deny"]) or organization in scope.get("denied_ids", []):
        return False
    if not within_company_boundary(user, doc):
        return False
    # Organizational oversight does not automatically expose individual HR records.
    organizational_grant = organization in scope.get("allowed_ids", []) and not doc.get(
        "facts", {}
    ).get("subject_id")
    return (
        organization in user.get("administrative_read_scope", [])
        or organizational_grant
        or bool(principals.intersection(doc["allow"]))
    )


def within_company_boundary(user, doc):
    company = doc.get("applies_company")
    scope = user.get("organization_read_scope", {})
    return (
        not company
        or company == user.get("company_id")
        or company in user.get("administrative_read_scope", [])
        or company in scope.get("allowed_ids", [])
        or doc.get("applies_department") in scope.get("allowed_ids", [])
    )


def organization_read_scope(conn, user):
    organizations = [dict(r) for r in conn.execute("SELECT * FROM organizations ORDER BY id")]
    edges = {o["id"]: [o["parent_id"]] if o["parent_id"] else [] for o in organizations}
    try:
        _no_cycles(edges)
    except ValueError:
        return {
            "allowed_ids": [],
            "denied_ids": [o["id"] for o in organizations],
            "bindings": [],
            "group_root": None,
        }
    subjects = binding_subjects(user)
    bindings = [
        dict(r)
        for r in conn.execute("SELECT * FROM organization_read_bindings ORDER BY id")
        if r["subject"] in subjects
    ]
    scopes = {"allow": set(), "deny": set()}
    for binding in bindings:
        ids = {binding["organization_id"]}
        if binding["include_descendants"]:
            while True:
                expanded = ids | {o["id"] for o in organizations if o["parent_id"] in ids}
                if expanded == ids:
                    break
                ids = expanded
        scopes[binding["effect"]].update(ids)
    roots = [o["id"] for o in organizations if o["level"] == "group" and not o["parent_id"]]
    return {
        "allowed_ids": sorted(scopes["allow"] - scopes["deny"]),
        "denied_ids": sorted(scopes["deny"]),
        "bindings": bindings,
        "group_root": roots[0] if len(roots) == 1 else None,
    }


def bindings_for(conn: sqlite3.Connection) -> list[dict]:
    organizations = [dict(r) for r in conn.execute("SELECT * FROM organizations")]
    try:
        _no_cycles({o["id"]: [o["parent_id"]] if o["parent_id"] else [] for o in organizations})
        valid_tree = True
    except ValueError:
        valid_tree = False
    bindings = [dict(row) for row in conn.execute("SELECT * FROM role_bindings ORDER BY id")]
    for binding in bindings:
        ids = {binding["scope_company"]} if valid_tree else set()
        if valid_tree and binding.get("include_descendants") and binding["scope_company"]:
            while True:
                expanded = ids | {o["id"] for o in organizations if o["parent_id"] in ids}
                if expanded == ids:
                    break
                ids = expanded
        binding["_scope_ids"] = sorted(i for i in ids if i is not None)
        binding["_company_ids"] = [
            o["id"]
            for o in organizations
            if o["level"] == "company" and (binding["scope_company"] is None or o["id"] in ids)
        ]
        binding["_create_scopes"] = [
            {
                "company_id": o["id"] if o["level"] == "company" else o["parent_id"],
                "department_id": o["id"] if o["level"] == "department" else None,
            }
            for o in organizations
            if o["level"] in ("company", "department")
            and (binding["scope_company"] is None or o["id"] in ids)
        ]
        roots = [o["id"] for o in organizations if o["level"] == "group" and not o["parent_id"]]
        binding["_group_root"] = roots[0] if len(roots) == 1 else None
    return bindings


def roles_for(conn):
    return {row["id"]: row["name"] for row in conn.execute("SELECT id,name FROM roles ORDER BY id")}


def binding_subjects(user):
    subjects = {"user:" + user["id"]} | {"group:" + group for group in user["groups"]}
    if user.get("role_registered"):
        subjects.add("role:" + user["role"])
    return subjects


def can_action(user: dict, action: str, bindings: list[dict], doc: dict | None = None) -> bool:
    if doc and not within_company_boundary(user, doc):
        return False
    subjects = binding_subjects(user)
    contextual = doc is None and action.startswith(("query.", "feedback.", "system."))

    def matches_scope(binding):
        if binding["scope_company"] is None:
            return True
        ids = binding.get("_scope_ids", [binding["scope_company"]])
        if contextual:
            return user.get("company_id") in ids
        if doc is None:
            return True
        targets = {
            doc.get("applies_department")
            or doc.get("applies_company")
            or binding.get("_group_root"),
            doc.get("applies_company"),
        }
        if action in ("document.catalog", "document.review_department", "audit.read"):
            targets.update(
                r["department_id"]
                for r in doc.get("review", {}).get("departments", [])
                if r["status"] == "pending" and doc["status"] == "staging"
            )
        return bool(targets.intersection(ids))

    matched = [
        binding
        for binding in bindings
        if binding["subject"] in subjects and binding["action"] == action and matches_scope(binding)
    ]
    if any(binding["effect"] == "deny" for binding in matched):
        return False
    if doc and (
        principals_for(user).intersection(doc.get("deny", []))
        or (doc.get("applies_department") or doc.get("applies_company"))
        in user.get("organization_read_scope", {}).get("denied_ids", [])
    ):
        return False
    administration = any(
        b["subject"] in subjects
        and b["action"] == "document.manage_scope"
        and b["effect"] == "allow"
        and (
            b["scope_company"] is None
            or (
                doc
                and (
                    doc.get("applies_department")
                    or doc.get("applies_company")
                    or b.get("_group_root")
                )
                in b.get("_scope_ids", [])
            )
        )
        for b in bindings
    )
    administration_denied = any(
        b["subject"] in subjects
        and b["action"] == "document.manage_scope"
        and b["effect"] == "deny"
        and (
            b["scope_company"] is None
            or (
                doc
                and (
                    doc.get("applies_department")
                    or doc.get("applies_company")
                    or b.get("_group_root")
                )
                in b.get("_scope_ids", [])
            )
        )
        for b in bindings
    )
    if administration_denied:
        administration = False
    scoped_review = (
        doc is not None and doc.get("status") == "staging" and action.startswith("document.review_")
    )
    pending_review_catalog = (
        doc is not None
        and action == "document.catalog"
        and doc.get("status") == "staging"
        and doc.get("review", {}).get("stage") == "departments"
        and any(
            can_action(
                user, "document.review_department", bindings, {**doc, "_catalog_check": True}
            )
            for _ in [0]
        )
    )
    pending_owner = bool(
        doc
        and doc.get("status") == "staging"
        and doc.get("owner_id") == user["id"]
        and not principals_for(user).intersection(doc.get("deny", []))
    )
    return any(binding["effect"] == "allow" for binding in matched) and (
        doc is None
        or can_access(user, doc)
        or pending_owner
        or administration
        or scoped_review
        or pending_review_catalog
    )


def can_create_somewhere(user, bindings):
    # A deny for company B must not hide an explicit create grant for company A.
    subjects = binding_subjects(user)
    scopes = {
        (scope["company_id"], scope["department_id"])
        for binding in bindings
        if binding["subject"] in subjects
        and binding["action"] == "document.create_version"
        and binding["effect"] == "allow"
        for scope in binding.get(
            "_create_scopes", [{"company_id": binding["scope_company"], "department_id": None}]
        )
    }
    return any(
        can_action(
            user,
            "document.create_version",
            bindings,
            {
                "applies_company": company,
                "applies_department": department,
                "status": "staging",
                "owner_id": user["id"],
                "allow": [],
                "deny": [],
            },
        )
        for company, department in scopes
    )


def can_create_group(conn, user, bindings):
    """Group-wide import requires an explicit root grant and authority in every subsidiary."""
    roots = [r[0] for r in conn.execute("SELECT id FROM organizations WHERE level='group' AND parent_id IS NULL")]
    if len(roots) != 1:
        return False
    draft = {
        "applies_company": None,
        "applies_department": None,
        "status": "staging",
        "owner_id": user["id"],
        "allow": [],
        "deny": [],
    }
    if not can_action(user, "document.create_version", bindings, draft):
        return False
    companies = [r[0] for r in conn.execute("SELECT id FROM organizations WHERE level='company'")]
    return bool(companies) and all(
        can_action(user, "document.create_version", bindings, {**draft, "applies_company": company})
        for company in companies
    )


def access_tree(conn):
    organizations = [dict(row) for row in conn.execute("SELECT * FROM organizations ORDER BY id")]
    people = [
        dict(row)
        for row in conn.execute("SELECT id,name,company_id,department_id FROM users ORDER BY name")
    ]
    return {
        "groups": [
            {
                "id": org["id"],
                "name": org["name"],
                "people": [p for p in people if p["company_id"] == org["id"]],
            }
            for org in organizations
            if org["level"] == "group"
        ],
        "companies": [
            {
                "id": org["id"],
                "name": org["name"],
                "parent_id": org["parent_id"],
                "departments": [
                    {
                        "id": dep["id"],
                        "name": dep["name"],
                        "people": [p for p in people if p["department_id"] == dep["id"]],
                    }
                    for dep in organizations
                    if dep["level"] == "department" and dep["parent_id"] == org["id"]
                ],
                "people": [
                    p for p in people if p["company_id"] == org["id"] and not p["department_id"]
                ],
            }
            for org in organizations
            if org["level"] == "company"
        ],
        "unassigned": [p for p in people if not p["company_id"]],
    }


def valid_read_principals(conn):
    groups = {
        g for row in conn.execute("SELECT groups_json FROM users") for g in json.loads(row[0])
    }
    groups.update(
        g
        for row in conn.execute("SELECT allow_json,deny_json FROM documents")
        for cell in row
        for g in json.loads(cell)
    )
    units = {
        ("company:" if row["level"] == "company" else "department:") + row["id"]
        for row in conn.execute(
            "SELECT id,level FROM organizations WHERE level IN ('company','department')"
        )
    }
    return groups | units | {"user:" + row[0] for row in conn.execute("SELECT id FROM users")}


def capabilities(user: dict, bindings: list[dict], docs: list[dict] | None = None) -> list[str]:
    actions = {binding["action"] for binding in bindings}
    return sorted(
        action
        for action in actions
        if action.startswith(("query.", "feedback.", "system."))
        and can_action(user, action, bindings)
    )


def all_capabilities(user: dict, bindings: list[dict], docs: list[dict]) -> list[str]:
    global_actions = capabilities(user, bindings)
    doc_actions = {
        binding["action"]
        for binding in bindings
        if not binding["action"].startswith(("query.", "feedback.", "system."))
    }
    actions = set(global_actions) | {
        action
        for action in doc_actions
        if any(can_action(user, action, bindings, doc) for doc in docs)
    }
    if can_create_somewhere(user, bindings):
        actions.add("document.create_version")
    return sorted(actions)


def applicable(user: dict, doc: dict, as_of: str) -> bool:
    # Applicability is business metadata; availability is separately checked by can_read.
    # An explicitly required rule becoming unavailable must not silently fall back to a base rule.
    if doc["effective_from"] > as_of:
        return False
    if doc["effective_to"] and doc["effective_to"] < as_of:
        return False
    return scope_matches(user, doc)


def scope_matches(user, doc):
    return all(
        (field == "applies_role" and user.get("_policy_lookup"))
        or not doc[field]
        or doc[field] == user[user_field]
        for field, user_field in (
            ("applies_company", "company_id"),
            ("applies_department", "department_id"),
            ("applies_role", "role"),
        )
    )


def today_local():
    return datetime.now(timezone(timedelta(hours=7))).date().isoformat()


def validate_date(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Ngày phải đúng định dạng YYYY-MM-DD và tồn tại trong lịch.")
    return date.fromisoformat(value)


def superseded_ids(user, docs, relations, as_of):
    sources = {d["id"]: d for d in docs}
    # A confirmed replacement does not resurrect its predecessor on expiry/withdrawal.
    return {
        r["target_id"]
        for r in relations
        if r["type"] == "supersedes"
        and r.get("approved", 1)
        and (source := sources.get(r["source_id"]))
        and source["approval"] == "approved"
        and source["status"] in ("published", "withdrawn")
        and source["effective_from"] <= as_of
        and scope_matches(user, source)
    }


def query_candidates(user, docs, relations, as_of):
    retired = superseded_ids(user, docs, relations, as_of)
    return [
        d
        for d in docs
        if d["id"] not in retired
        and d["approval"] == "approved"
        and d["status"] in ("published", "withdrawn")
        and applicable(user, d, as_of)
    ]


def document_superseded(doc, docs, relations, as_of):
    # Lifecycle belongs to the document, not to the reader's business department.
    # A replacement with the same scope retires the whole version for HR reviewers too.
    by_id = {d["id"]: d for d in docs}
    fields = ("policy_key", "applies_company", "applies_department", "applies_role")
    return any(
        r["type"] == "supersedes"
        and r.get("approved", 1)
        and r["target_id"] == doc["id"]
        and (source := by_id.get(r["source_id"]))
        and source["approval"] == "approved"
        and source["status"] in ("published", "withdrawn")
        and source["effective_from"] <= as_of
        and all(source[field] == doc[field] for field in fields)
        for r in relations
    )


def validity_info(doc, as_of, user, docs, relations):
    if doc["status"] == "withdrawn":
        state = "withdrawn"
    elif doc["effective_from"] > as_of:
        state = "upcoming"
    elif doc["effective_to"] and doc["effective_to"] < as_of:
        state = "expired"
    elif document_superseded(doc, docs, relations, as_of):
        state = "superseded"
    elif doc["status"] != "published" or doc["approval"] != "approved":
        state = "draft"
    else:
        state = "effective"
    labels = {
        "withdrawn": "Đã thu hồi",
        "upcoming": "Chưa đến hiệu lực",
        "expired": "Hết hiệu lực",
        "superseded": "Đã được thay thế",
        "draft": "Chưa sẵn sàng hỏi đáp",
        "effective": "Đang hiệu lực",
    }
    return {
        "validity": state,
        "validity_label": labels[state],
        "validity_as_of": as_of,
        "query_eligible": can_read(user, doc)
        and applicable(user, doc, as_of)
        and doc["id"] not in superseded_ids(user, docs, relations, as_of),
    }
