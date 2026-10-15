"""Install the explicit IT configuration permission once; preserve all user data."""

import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.store import DEFAULT_DB, ROOT, connect

with connect(DEFAULT_DB) as conn:
    if conn.execute("SELECT 1 FROM settings WHERE key='llm_api_permission_v1'").fetchone():
        print("API permission already installed.")
    else:
        backup = ROOT / "runtime/backups" / ("before-api-" + uuid.uuid4().hex[:8] + ".sqlite3")
        backup.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(backup) as target:
            conn.backup(target)
        if not conn.execute(
            "SELECT 1 FROM role_bindings WHERE action='system.configure_llm'"
        ).fetchone():
            conn.execute(
                "INSERT INTO role_bindings(subject,action,effect,scope_company,include_descendants) VALUES('role:it_security','system.configure_llm','allow',NULL,0)"
            )
        conn.execute("INSERT INTO settings VALUES('llm_api_permission_v1','installed')")
        print("Installed database permission system.configure_llm for IT. Backup:", backup.name)
