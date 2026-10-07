import json
import re
import sqlite3
from pathlib import Path

from .domain import ConflictError, ValidationError
from .repository import utcnow


_INSTITUTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class LocalLedgerStore:
    """按机构编号管理各机构本地 SQLite 账文件。"""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._cache = {}

    def get(self, institution_id):
        if not _INSTITUTION_ID.match(str(institution_id)):
            raise ValidationError("invalid institution id: " + str(institution_id))
        if institution_id not in self._cache:
            self._cache[institution_id] = LocalLedger(
                institution_id, self.directory / ("%s.db" % institution_id)
            )
        return self._cache[institution_id]

    def __call__(self, institution_id):
        return self.get(institution_id)


class LocalLedger:
    """机构本地授权账。

    断网期间机构照常在本地登记授权、办理取用(activate)和撤回(revoke)，
    操作以 pending 状态落本地表，网络恢复后交联盟服务按授权编号对账。
    """

    def __init__(self, institution_id, path):
        self.institution_id = institution_id
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS local_grants (
                    grant_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    policy_version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_ops (
                    op_id TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL,
                    op_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reconciled_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_pending_ops_state
                    ON pending_ops(state, op_id);
            """)

    @staticmethod
    def _grant_from_row(row):
        return {
            "grant_id": row["grant_id"],
            "status": row["status"],
            "policy_version": int(row["policy_version"]),
            "data": json.loads(row["data"]),
            "registered_at": row["registered_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _op_from_row(row):
        return {
            "op_id": row["op_id"],
            "grant_id": row["grant_id"],
            "op_type": row["op_type"],
            "payload": json.loads(row["payload"]),
            "state": row["state"],
            "created_at": row["created_at"],
            "reconciled_at": row["reconciled_at"],
        }

    def register_grant(self, grant_id, data=None, policy_version=1):
        now = utcnow()
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO local_grants(grant_id, status, policy_version, data, registered_at, updated_at) "
                    "VALUES (?, 'issued', ?, ?, ?, ?)",
                    (
                        grant_id,
                        policy_version,
                        json.dumps(data or {}, ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("grant already registered locally: " + grant_id)
        return self.get_grant(grant_id)

    def get_grant(self, grant_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM local_grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
        return self._grant_from_row(row) if row else None

    def list_grants(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM local_grants ORDER BY registered_at, grant_id"
            ).fetchall()
        return [self._grant_from_row(row) for row in rows]

    def _queue_op(self, connection, grant_id, op_type, payload):
        # 同一授权同一类型只保留一条待对账操作，断网期间重复办理不会重复入账。
        row = connection.execute(
            "SELECT op_id FROM pending_ops WHERE grant_id = ? AND op_type = ? AND state = 'pending'",
            (grant_id, op_type),
        ).fetchone()
        if row:
            raise ConflictError(
                "pending %s for grant %s already queued" % (op_type, grant_id)
            )
        op_id = "%s:%s:%s" % (self.institution_id, op_type, grant_id)
        now = utcnow()
        connection.execute(
            "INSERT INTO pending_ops(op_id, grant_id, op_type, payload, state, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (
                op_id,
                grant_id,
                op_type,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        return op_id

    def activate(self, grant_id, starts_at, expires_at):
        if expires_at < starts_at:
            raise ValidationError("grant expiry must be after start")
        with self._connect() as connection:
            self._require_issued(connection, grant_id)
            op_id = self._queue_op(
                connection,
                grant_id,
                "activate",
                {"starts_at": starts_at, "expires_at": expires_at},
            )
        return op_id

    def revoke(self, grant_id, reason):
        if not str(reason or "").strip():
            raise ValidationError("reason is required")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM local_grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
            if not row:
                raise ValidationError("unknown local grant: " + grant_id)
            if row["status"] == "revoked":
                raise ConflictError("grant already revoked locally: " + grant_id)
            op_id = self._queue_op(
                connection, grant_id, "revoke", {"reason": reason}
            )
        return op_id

    @staticmethod
    def _require_issued(connection, grant_id):
        row = connection.execute(
            "SELECT status FROM local_grants WHERE grant_id = ?", (grant_id,)
        ).fetchone()
        if not row:
            raise ValidationError("unknown local grant: " + grant_id)
        if row["status"] != "issued":
            raise ConflictError(
                "cannot queue activation: grant %s is %s" % (grant_id, row["status"])
            )

    def list_pending_ops(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pending_ops WHERE state = 'pending' ORDER BY created_at, op_id"
            ).fetchall()
        return [self._op_from_row(row) for row in rows]

    def list_ops(self, state=None):
        query = "SELECT * FROM pending_ops"
        params = []
        if state:
            query += " WHERE state = ?"
            params.append(state)
        query += " ORDER BY created_at, op_id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._op_from_row(row) for row in rows]

    def mark_op(self, op_id, state):
        with self._connect() as connection:
            connection.execute(
                "UPDATE pending_ops SET state = ?, reconciled_at = ? WHERE op_id = ?",
                (state, utcnow(), op_id),
            )

    def sync_grant(self, grant_id, status, policy_version, data=None):
        """对账后按主账结果回写本地授权状态与策略版本。"""
        now = utcnow()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM local_grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
            if not row:
                return None
            merged = json.loads(row["data"])
            if data:
                merged.update(data)
            connection.execute(
                "UPDATE local_grants SET status = ?, policy_version = ?, data = ?, updated_at = ? "
                "WHERE grant_id = ?",
                (
                    status,
                    policy_version,
                    json.dumps(merged, ensure_ascii=False, sort_keys=True),
                    now,
                    grant_id,
                ),
            )
        return self.get_grant(grant_id)
