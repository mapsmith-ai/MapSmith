"""Durable job ledger on Postgres (optional).

Every tool call is modeled as a job row from day one (id, operation, params,
status, artifact, manifest). Execution stays in-process for now; the same
table will back the MCP Tasks extension and a worker queue later — designed-in
seam, deferred infrastructure.

If DATABASE_URL is unset (or psycopg is not installed) the ledger is a no-op:
local/stdio users need zero infrastructure.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS mapsmith_jobs (
    id UUID PRIMARY KEY,
    operation TEXT NOT NULL,
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    artifact TEXT,
    manifest JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
"""

_schema_ready = False


def _connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg
    except ImportError:
        return None
    try:
        return psycopg.connect(url, autocommit=True)
    except Exception:  # noqa: BLE001 — the ledger must never take the server down
        return None


def _ensure_schema(conn) -> None:
    global _schema_ready
    if not _schema_ready:
        conn.execute(_DDL)
        _schema_ready = True


@contextmanager
def job(operation: str, params: dict[str, Any]):
    """Record a tool execution as a durable job row (no-op without DATABASE_URL)."""
    conn = _connect()
    job_id = str(uuid.uuid4())
    if conn is not None:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO mapsmith_jobs (id, operation, params) VALUES (%s, %s, %s)",
            (job_id, operation, json.dumps(params, default=str)),
        )
    try:
        result: dict[str, Any] = {}
        yield job_id, result
    except Exception as exc:
        if conn is not None:
            conn.execute(
                "UPDATE mapsmith_jobs SET status='failed', error=%s, finished_at=now() "
                "WHERE id=%s",
                (str(exc)[:2000], job_id),
            )
            conn.close()
        raise
    if conn is not None:
        conn.execute(
            "UPDATE mapsmith_jobs SET status='completed', artifact=%s, finished_at=now() "
            "WHERE id=%s",
            (result.get("output"), job_id),
        )
        conn.close()
