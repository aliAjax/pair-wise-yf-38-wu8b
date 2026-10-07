import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS grant_ownership (
                    grant_id TEXT PRIMARY KEY,
                    institution_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    claimed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reconciliation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    institution_id TEXT NOT NULL,
                    grant_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_recon_institution
                    ON reconciliation_log(institution_id, id);
                CREATE TABLE IF NOT EXISTS institution_merges (
                    merge_id TEXT PRIMARY KEY,
                    from_institution TEXT NOT NULL,
                    to_institution TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    processed INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS institution_merge_items (
                    merge_id TEXT NOT NULL,
                    grant_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    PRIMARY KEY(merge_id, grant_id)
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    # ------------------------------------------------------------------
    # Federation: grant ownership claims (first writer wins)
    # ------------------------------------------------------------------
    def claim_grant_ownership(self, grant_id, institution_id):
        """原子认领授权归属；已有他机构认领时抛 ConflictError（先落主账者胜）。"""
        now = utcnow()
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO grant_ownership(grant_id, institution_id, version, claimed_at) "
                    "VALUES (?, ?, 1, ?)",
                    (grant_id, institution_id, now),
                )
                return "claimed"
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT institution_id FROM grant_ownership WHERE grant_id = ?",
                    (grant_id,),
                ).fetchone()
                if row and row["institution_id"] == institution_id:
                    return "already_owned"
                raise ConflictError(
                    "grant %s already reconciled by another institution" % grant_id
                )

    def get_owner(self, grant_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT institution_id FROM grant_ownership WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
        return row["institution_id"] if row else None

    def list_owned_grants(self, institution_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT grant_id FROM grant_ownership WHERE institution_id = ? ORDER BY grant_id",
                (institution_id,),
            ).fetchall()
        return [row["grant_id"] for row in rows]

    def append_reconciliation(self, institution_id, grant_id, action, outcome, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO reconciliation_log(institution_id, grant_id, action, outcome, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    institution_id,
                    grant_id,
                    action,
                    outcome,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_reconciliation(self, institution_id=None):
        with self._connect() as connection:
            if institution_id:
                rows = connection.execute(
                    "SELECT * FROM reconciliation_log WHERE institution_id = ? ORDER BY id",
                    (institution_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM reconciliation_log ORDER BY id"
                ).fetchall()
        return [
            {
                "id": row["id"],
                "institution_id": row["institution_id"],
                "grant_id": row["grant_id"],
                "action": row["action"],
                "outcome": row["outcome"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Federation: institution merge checkpoints (resumable)
    # ------------------------------------------------------------------
    def create_merge(self, merge_id, from_institution, to_institution, grant_ids):
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO institution_merges(merge_id, from_institution, to_institution, "
                    "status, total, processed, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'pending', ?, 0, ?, ?)",
                    (merge_id, from_institution, to_institution, len(grant_ids), now, now),
                )
            except sqlite3.IntegrityError:
                # 重复提交同一合并任务不会产生第二份迁移
                raise ConflictError("merge already submitted: " + merge_id)
            connection.executemany(
                "INSERT INTO institution_merge_items(merge_id, grant_id, state) VALUES (?, ?, 'pending')",
                [(merge_id, grant_id) for grant_id in grant_ids],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_merge(merge_id)

    def get_merge(self, merge_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM institution_merges WHERE merge_id = ?", (merge_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "merge_id": row["merge_id"],
            "from_institution": row["from_institution"],
            "to_institution": row["to_institution"],
            "status": row["status"],
            "total": int(row["total"]),
            "processed": int(row["processed"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_merge_items(self, merge_id, state=None):
        query = "SELECT grant_id, state FROM institution_merge_items WHERE merge_id = ?"
        params = [merge_id]
        if state:
            query += " AND state = ?"
            params.append(state)
        query += " ORDER BY grant_id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [{"grant_id": row["grant_id"], "state": row["state"]} for row in rows]

    def mark_merge_item(self, merge_id, grant_id, state):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "UPDATE institution_merge_items SET state = ? WHERE merge_id = ? AND grant_id = ?",
                (state, merge_id, grant_id),
            )
            connection.execute(
                "UPDATE institution_merges SET processed = "
                "(SELECT COUNT(*) FROM institution_merge_items WHERE merge_id = ? AND state != 'pending'), "
                "status = CASE "
                "WHEN EXISTS (SELECT 1 FROM institution_merge_items WHERE merge_id = ? AND state = 'pending') "
                "THEN 'in_progress' ELSE 'completed' END, "
                "updated_at = ? WHERE merge_id = ?",
                (merge_id, merge_id, now, merge_id),
            )
        return self.get_merge(merge_id)

    def transfer_ownership(self, grant_id, to_institution):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "UPDATE grant_ownership SET institution_id = ?, version = version + 1, claimed_at = ? "
                "WHERE grant_id = ?",
                (to_institution, now, grant_id),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
