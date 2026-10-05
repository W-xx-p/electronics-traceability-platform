from __future__ import annotations

import json
import hashlib
import hmac
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .context import request_context


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path, audit_hmac_key: str = ""):
        self.path = Path(path)
        self.audit_hmac_key = audit_hmac_key.encode()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS product_models (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    rules_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS production_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_number TEXT NOT NULL UNIQUE,
                    product_model_id INTEGER NOT NULL,
                    batch_number TEXT NOT NULL,
                    target_quantity INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(product_model_id) REFERENCES product_models(id)
                );

                CREATE TABLE IF NOT EXISTS inspections (
                    id TEXT PRIMARY KEY,
                    order_id INTEGER NOT NULL,
                    product_model_id INTEGER NOT NULL,
                    serial_number TEXT,
                    batch_number TEXT,
                    recognized_model TEXT,
                    status TEXT NOT NULL,
                    overall_confidence REAL NOT NULL,
                    image_path TEXT NOT NULL,
                    fields_json TEXT NOT NULL,
                    qr_payload_json TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    quality_json TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES production_orders(id),
                    FOREIGN KEY(product_model_id) REFERENCES product_models(id)
                );

                CREATE INDEX IF NOT EXISTS idx_inspection_serial
                    ON inspections(serial_number);
                CREATE INDEX IF NOT EXISTS idx_inspection_order
                    ON inspections(order_id);

                CREATE TABLE IF NOT EXISTS review_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inspection_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'open',
                    resolution TEXT,
                    corrected_fields_json TEXT,
                    reviewer TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    FOREIGN KEY(inspection_id) REFERENCES inspections(id)
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'quality_engineer', 'operator', 'viewer')),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auth_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );

                CREATE INDEX IF NOT EXISTS idx_auth_token ON auth_sessions(token_hash);
                CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_logs(entity_type, entity_id);

                CREATE TABLE IF NOT EXISTS workstations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    location TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    last_seen_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS product_rule_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_model_id INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    rules_json TEXT NOT NULL,
                    change_reason TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(product_model_id, version),
                    FOREIGN KEY(product_model_id) REFERENCES product_models(id)
                );

                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    description TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS outbox_events (
                    id TEXT PRIMARY KEY,
                    aggregate_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    processed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_outbox_status
                    ON outbox_events(status, created_at);
                """
            )
            self._add_column(conn, "inspections", "workstation_id", "INTEGER REFERENCES workstations(id)")
            self._add_column(conn, "inspections", "processing_ms", "INTEGER NOT NULL DEFAULT 0")
            self._add_column(conn, "inspections", "image_sha256", "TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "inspections", "version", "INTEGER NOT NULL DEFAULT 1")
            self._add_column(conn, "review_tasks", "claimed_by", "TEXT")
            self._add_column(conn, "review_tasks", "claimed_at", "TEXT")
            self._add_column(conn, "review_tasks", "version", "INTEGER NOT NULL DEFAULT 1")
            self._add_column(conn, "users", "failed_attempts", "INTEGER NOT NULL DEFAULT 0")
            self._add_column(conn, "users", "locked_until", "TEXT")
            self._add_column(conn, "users", "last_login_at", "TEXT")
            self._add_column(conn, "workstations", "api_key_hash", "TEXT")
            self._add_column(conn, "workstations", "software_version", "TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "audit_logs", "previous_hash", "TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "audit_logs", "record_hash", "TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_inspection_created ON inspections(created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_inspection_status ON inspections(status)")
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS uq_passed_serial
                   ON inspections(serial_number)
                   WHERE status = 'pass' AND serial_number IS NOT NULL"""
            )
            conn.execute(
                """INSERT INTO product_rule_versions(
                       product_model_id, version, rules_json, change_reason, created_by, created_at
                   )
                   SELECT p.id, 1, p.rules_json, 'migrated initial configuration', 'system', p.created_at
                   FROM product_models p
                   WHERE NOT EXISTS (
                       SELECT 1 FROM product_rule_versions v WHERE v.product_model_id = p.id
                   )"""
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, description, applied_at) VALUES (1, ?, ?)",
                ("enterprise foundation", utc_now()),
            )
            self._backfill_audit_hashes(conn)

    @staticmethod
    def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _hash_audit(self, payload: str) -> str:
        if self.audit_hmac_key:
            return hmac.new(self.audit_hmac_key, payload.encode(), hashlib.sha256).hexdigest()
        return hashlib.sha256(payload.encode()).hexdigest()

    def _backfill_audit_hashes(self, conn: sqlite3.Connection) -> None:
        previous = "GENESIS"
        rows = conn.execute(
            "SELECT id, entity_type, entity_id, action, details_json, created_at, record_hash FROM audit_logs ORDER BY id"
        ).fetchall()
        for row in rows:
            if not row["record_hash"]:
                payload = "|".join(str(row[key]) for key in ("id", "entity_type", "entity_id", "action", "details_json", "created_at"))
                record_hash = self._hash_audit(f"{previous}|{payload}")
                conn.execute(
                    "UPDATE audit_logs SET previous_hash = ?, record_hash = ? WHERE id = ?",
                    (previous, record_hash, row["id"]),
                )
                previous = record_hash
            else:
                previous = row["record_hash"]

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.connect() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def audit(self, entity_type: str, entity_id: str, action: str, details: dict[str, Any]) -> None:
        enriched = {**details}
        for key in ("request_id", "client_ip", "actor"):
            if value := request_context.get().get(key):
                enriched.setdefault(key, value)
        details_json = json.dumps(enriched, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        created_at = utc_now()
        with self.connect() as conn:
            # Serialize the read-head/append sequence so concurrent requests
            # cannot create two audit records with the same previous hash.
            conn.execute("BEGIN IMMEDIATE")
            previous_row = conn.execute("SELECT record_hash FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()
            previous = previous_row["record_hash"] if previous_row and previous_row["record_hash"] else "GENESIS"
            cursor = conn.execute(
                """INSERT INTO audit_logs(
                       entity_type, entity_id, action, details_json, created_at, previous_hash, record_hash
                   ) VALUES (?, ?, ?, ?, ?, ?, '')""",
                (entity_type, entity_id, action, details_json, created_at, previous),
            )
            audit_id = int(cursor.lastrowid)
            payload = "|".join((str(audit_id), entity_type, entity_id, action, details_json, created_at))
            record_hash = self._hash_audit(f"{previous}|{payload}")
            conn.execute("UPDATE audit_logs SET record_hash = ? WHERE id = ?", (record_hash, audit_id))

    def verify_audit_chain(self) -> dict[str, Any]:
        previous = "GENESIS"
        rows = self.fetch_all(
            "SELECT id, entity_type, entity_id, action, details_json, created_at, previous_hash, record_hash FROM audit_logs ORDER BY id"
        )
        for row in rows:
            payload = "|".join(str(row[key]) for key in ("id", "entity_type", "entity_id", "action", "details_json", "created_at"))
            expected = self._hash_audit(f"{previous}|{payload}")
            if row["previous_hash"] != previous or not hmac.compare_digest(row["record_hash"], expected):
                return {"valid": False, "broken_at_id": row["id"], "records": len(rows)}
            previous = row["record_hash"]
        return {"valid": True, "broken_at_id": None, "records": len(rows), "head_hash": previous}


def decode_json_columns(row: dict[str, Any] | None, columns: tuple[str, ...]) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for column in columns:
        raw = result.get(column)
        if raw is not None:
            result[column.removesuffix("_json")] = json.loads(raw)
            del result[column]
    return result
