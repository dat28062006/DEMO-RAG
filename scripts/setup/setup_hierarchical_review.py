"""Back up the current DB and install explicitly authorized hierarchy grants once."""

from pathlib import Path
import sys, json, uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.store import DEFAULT_DB, ROOT, SEED_FILE, connect, initialize, document_records
from app.review_workflow import ensure_review


def migrate(db_path=DEFAULT_DB, backup_dir=None):
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    backup = None
    if Path(db_path).exists():
        with connect(db_path) as conn:
            if conn.execute(
                "SELECT 1 FROM settings WHERE key='hierarchical_review_migration_v1'"
            ).fetchone():
                return {"already_applied": True}
        backup = Path(backup_dir or ROOT / "runtime" / "backups") / (
            "before-hierarchical-review-" + uuid.uuid4().hex[:8] + ".sqlite3"
        )
        backup.parent.mkdir(parents=True, exist_ok=True)
        with connect(db_path) as src, connect(backup) as dst:
            src.backup(dst)
    initialize(db_path)
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for role in seed["roles"]:
            conn.execute("INSERT OR IGNORE INTO roles VALUES(?,?)", (role["id"], role["name"]))
        for person in seed["users"]:
            if person["role"] not in ("company_director", "department_head"):
                continue
            existing = conn.execute(
                "SELECT company_id,department_id,role FROM users WHERE id=?", (person["id"],)
            ).fetchone()
            expected = (person["company_id"], person["department_id"], person["role"])
            if existing and tuple(existing) != expected:
                raise ValueError(
                    "New demo profile ID conflicts with an existing profile: " + person["id"]
                )
            conn.execute(
                "INSERT OR IGNORE INTO users VALUES(?,?,?,?,?,?,?)",
                (
                    person["id"],
                    person["name"],
                    person["company_id"],
                    person["department_id"],
                    person["role"],
                    json.dumps(person["groups"]),
                    person["description"],
                ),
            )
        for b in seed["role_bindings"]:
            if b["subject"] != "role:chairman" and b["subject"] not in {
                "user:U08",
                "user:U09",
                "user:U10",
                "user:U11",
                "user:U12",
                "user:U13",
            }:
                continue
            values = (
                b["subject"],
                b["action"],
                b["effect"],
                b["scope_company"],
                int(b.get("include_descendants", False)),
            )
            if not conn.execute(
                "SELECT 1 FROM role_bindings WHERE subject=? AND action=? AND effect=? AND scope_company IS ? AND include_descendants=?",
                values,
            ).fetchone():
                conn.execute(
                    "INSERT INTO role_bindings(subject,action,effect,scope_company,include_descendants) VALUES(?,?,?,?,?)",
                    values,
                )
        for b in seed["organization_read_bindings"]:
            conn.execute(
                "INSERT OR IGNORE INTO organization_read_bindings VALUES(?,?,?,?,?)",
                (
                    None,
                    b["subject"],
                    b["organization_id"],
                    int(b["include_descendants"]),
                    b["effect"],
                ),
            )
        for policy in seed["organization_review_policies"]:
            conn.execute(
                "INSERT OR REPLACE INTO organization_review_policies VALUES(?,?)",
                (policy["organization_id"], int(policy["enabled"])),
            )
        count = 0
        for doc in document_records(conn):
            if doc["status"] == "staging" and not doc.get("review"):
                if ensure_review(conn, doc):
                    count += 1
        conn.execute("INSERT INTO settings VALUES('hierarchical_review_migration_v1','applied')")
        conn.execute(
            "INSERT INTO audit(actor_id,document_id,action,occurred_at,detail_json) VALUES('system','SYSTEM','system.hierarchical_review',datetime('now'),?)",
            (
                json.dumps(
                    {
                        "backup": str(backup),
                        "staging_documents_routed": count,
                        "published_documents_preserved": True,
                    }
                ),
            ),
        )
    return {
        "backup": str(backup),
        "staging_documents_routed": count,
        "published_documents_preserved": True,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    print(json.dumps(migrate(parser.parse_args().db), ensure_ascii=False))
