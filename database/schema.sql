-- SQLite schema exported from app/store.py (SCHEMA).
-- Reference snapshot: application initialization still uses app/store.py.
-- Creates tables and indexes only; does not load demo or runtime data.
PRAGMA foreign_keys = ON;

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
