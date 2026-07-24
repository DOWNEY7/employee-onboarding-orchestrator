"""
api/run_store.py
─────────────────────────────────────────────────────────────────────────────
Persistent run registry backed by a SQLite table in the same database used by
the LangGraph SqliteSaver checkpointer.

Replaces the ephemeral in-memory ``_run_registry`` dict so that run metadata
(status, approval state, error messages) survives server restarts and is
available to all workers sharing the same SQLite file.

Thread-safety: all writes are protected by a ``threading.Lock``.

Schema
------
::

    run_records(
        run_id          TEXT PRIMARY KEY,
        mode            TEXT NOT NULL,         -- 'onboarding' | 'leaver'
        form_json       TEXT NOT NULL,         -- JSON-serialised intake form
        status          TEXT NOT NULL,         -- 'running'|'awaiting_approval'|'complete'|'failed'
        approval_status TEXT,                  -- 'pending' | 'approved' | 'rejected' | NULL
        error           TEXT,                  -- last agent error, if any
        created_at      TEXT NOT NULL,         -- ISO-8601 UTC timestamp
        updated_at      TEXT NOT NULL          -- ISO-8601 UTC timestamp
    )
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS run_records (
    run_id          TEXT PRIMARY KEY,
    mode            TEXT NOT NULL,
    form_json       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',
    approval_status TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


class RunStore:
    """
    Thread-safe SQLite-backed store for run metadata.

    Usage::

        conn = sqlite3.connect("onboarding.db", check_same_thread=False)
        store = RunStore(conn)

        record = store.create("run-123", "onboarding", form_dict)
        store.update("run-123", status="complete", approval_status="approved")
        record = store.get("run-123")
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        self._setup()

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _setup(self) -> None:
        """Create the run_records table if it doesn't exist."""
        with self._lock:
            self._conn.execute(_DDL)
            self._conn.commit()
        logger.info("RunStore: run_records table ready")

    # ── Write ops ─────────────────────────────────────────────────────────────

    def create(self, run_id: str, mode: str, form: dict[str, Any]) -> dict[str, Any]:
        """Insert a new run record and return it as a dict."""
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO run_records
                    (run_id, mode, form_json, status, approval_status, error,
                     created_at, updated_at)
                VALUES (?, ?, ?, 'running', NULL, NULL, ?, ?)
                """,
                (run_id, mode, json.dumps(form, default=str), now, now),
            )
            self._conn.commit()
        logger.info("RunStore: created run %s (mode=%s)", run_id, mode)
        result = self.get(run_id)
        assert result is not None  # just inserted
        return result

    def update(self, run_id: str, **kwargs: Any) -> None:
        """
        Update one or more fields on a run record.

        Allowed kwargs: ``status``, ``approval_status``, ``error``.
        ``updated_at`` is set automatically.
        """
        allowed = {"status", "approval_status", "error"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        now = datetime.now(UTC).isoformat()
        updates["updated_at"] = now
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [run_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE run_records SET {set_clause} WHERE run_id = ?",  # noqa: S608
                values,
            )
            self._conn.commit()

    # ── Read ops ──────────────────────────────────────────────────────────────

    def get(self, run_id: str) -> dict[str, Any] | None:
        """Retrieve a run record by run_id. Returns ``None`` if not found."""
        row = self._conn.execute(
            "SELECT run_id, mode, form_json, status, approval_status, error, "
            "created_at, updated_at FROM run_records WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def list_all(self) -> list[dict[str, Any]]:
        """Return all run records ordered by creation time (newest first)."""
        rows = self._conn.execute(
            "SELECT run_id, mode, form_json, status, approval_status, error, "
            "created_at, updated_at FROM run_records ORDER BY created_at DESC"
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: tuple) -> dict[str, Any]:
        run_id, mode, form_json, status, approval_status, error, created_at, updated_at = row
        return {
            "run_id": run_id,
            "mode": mode,
            "form": json.loads(form_json),
            "status": status,
            "approval_status": approval_status,
            "error": error,
            "created_at": created_at,
            "updated_at": updated_at,
        }
