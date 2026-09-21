"""Deterministic mock ERP state for the shortage-expedite MCP server."""
import hashlib
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utcnow():
    return datetime.now(timezone.utc)


def iso(value):
    return value.isoformat(timespec="seconds")


class ExpediteStore:
    def __init__(self, path, ttl_seconds=900):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = max(30, int(ttl_seconds))
        self.lock = threading.RLock()
        with self.db() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS expedite_operations(
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_hash TEXT NOT NULL,
                    material_id TEXT NOT NULL,
                    material_name TEXT NOT NULL,
                    supplier TEXT NOT NULL,
                    shortage_qty REAL NOT NULL,
                    due_days INTEGER NOT NULL,
                    expected_object_version TEXT NOT NULL,
                    prepare_token TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    external_ref TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_expedite_status
                ON expedite_operations(status, updated_at);
                """
            )

    @contextmanager
    def db(self):
        with self.lock:
            connection = sqlite3.connect(str(self.path), timeout=15)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @staticmethod
    def _payload_hash(payload):
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _public(row, reused=False):
        value = dict(row)
        value["reused"] = reused
        return value

    def prepare(self, idempotency_key, material_id, material_name, supplier,
                shortage_qty, due_days, expected_object_version):
        payload = {
            "material_id": str(material_id).strip(),
            "material_name": str(material_name).strip(),
            "supplier": str(supplier).strip(),
            "shortage_qty": float(shortage_qty),
            "due_days": int(due_days),
            "expected_object_version": str(expected_object_version).strip(),
        }
        key = str(idempotency_key).strip()
        if not key or not payload["material_id"] or not payload["supplier"]:
            raise ValueError("idempotency_key, material_id and supplier are required")
        if payload["shortage_qty"] <= 0:
            raise ValueError("shortage_qty must be greater than zero")
        if not payload["expected_object_version"]:
            raise ValueError("expected_object_version is required")
        fingerprint = self._payload_hash(payload)
        with self.db() as connection:
            previous = connection.execute(
                "SELECT * FROM expedite_operations WHERE idempotency_key=?", (key,)
            ).fetchone()
            if previous:
                if previous["payload_hash"] != fingerprint:
                    raise ValueError("idempotency key was already used with a different payload")
                return self._public(previous, reused=True)
            current = utcnow()
            operation_id = "ERP-" + secrets.token_hex(6).upper()
            prepare_token = "PREP-" + secrets.token_urlsafe(18)
            connection.execute(
                """INSERT INTO expedite_operations VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    operation_id, key, fingerprint, payload["material_id"],
                    payload["material_name"], payload["supplier"],
                    payload["shortage_qty"], payload["due_days"],
                    payload["expected_object_version"], prepare_token, "prepared",
                    iso(current + timedelta(seconds=self.ttl_seconds)), None,
                    iso(current), iso(current),
                ),
            )
            row = connection.execute(
                "SELECT * FROM expedite_operations WHERE id=?", (operation_id,)
            ).fetchone()
            return self._public(row)

    def commit(self, prepare_token, current_object_version):
        token = str(prepare_token).strip()
        with self.db() as connection:
            row = connection.execute(
                "SELECT * FROM expedite_operations WHERE prepare_token=?", (token,)
            ).fetchone()
            if not row:
                raise ValueError("prepare token does not exist")
            if row["status"] == "committed":
                return self._public(row, reused=True)
            if row["status"] != "prepared":
                raise ValueError("operation is not prepared")
            if datetime.fromisoformat(row["expires_at"]) <= utcnow():
                connection.execute(
                    "UPDATE expedite_operations SET status='expired',updated_at=? WHERE id=?",
                    (iso(utcnow()), row["id"]),
                )
                raise ValueError("prepared operation has expired")
            if str(current_object_version).strip() != row["expected_object_version"]:
                raise ValueError("business object changed after approval")
            external_ref = "EXP-" + secrets.token_hex(6).upper()
            connection.execute(
                """UPDATE expedite_operations
                SET status='committed',external_ref=?,updated_at=? WHERE id=?""",
                (external_ref, iso(utcnow()), row["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM expedite_operations WHERE id=?", (row["id"],)
            ).fetchone()
            return self._public(updated)

    def rollback(self, prepare_token):
        token = str(prepare_token).strip()
        with self.db() as connection:
            row = connection.execute(
                "SELECT * FROM expedite_operations WHERE prepare_token=?", (token,)
            ).fetchone()
            if not row:
                raise ValueError("prepare token does not exist")
            if row["status"] == "rolled_back":
                return self._public(row, reused=True)
            if row["status"] == "committed":
                raise ValueError("committed expedite cannot be rolled back by this mock contract")
            connection.execute(
                "UPDATE expedite_operations SET status='rolled_back',updated_at=? WHERE id=?",
                (iso(utcnow()), row["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM expedite_operations WHERE id=?", (row["id"],)
            ).fetchone()
            return self._public(updated)

    def status(self, operation_id="", idempotency_key=""):
        if not operation_id and not idempotency_key:
            raise ValueError("operation_id or idempotency_key is required")
        field, value = ("id", operation_id) if operation_id else ("idempotency_key", idempotency_key)
        with self.db() as connection:
            row = connection.execute(
                f"SELECT * FROM expedite_operations WHERE {field}=?", (str(value).strip(),)
            ).fetchone()
            if not row:
                raise ValueError("operation does not exist")
            return self._public(row)
