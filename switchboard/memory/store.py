"""Durable state: tickets, approvals, call records, reviews, and the ITSM outbox.

Plain SQLite on purpose. The caller-tier memory has to survive a process restart to
mean anything, and a dict in a module does not. Everything here is synchronous and
boring, which is what you want underneath a phone call.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from ..config import P

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
  id           TEXT PRIMARY KEY,
  employee_id  TEXT NOT NULL,
  path         TEXT NOT NULL,
  summary      TEXT NOT NULL,
  urgency      TEXT NOT NULL,
  steps_tried  TEXT NOT NULL DEFAULT '[]',
  status       TEXT NOT NULL DEFAULT 'open',
  call_id      TEXT,
  created_at   REAL NOT NULL,
  -- set when the ITSM backend was unreachable and this was written locally instead;
  -- the reconciler picks these up. See tools/itsm.py.
  pending_sync INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_tickets_employee ON tickets(employee_id, created_at DESC);

CREATE TABLE IF NOT EXISTS approvals (
  id           TEXT PRIMARY KEY,
  call_id      TEXT NOT NULL,
  employee_id  TEXT NOT NULL,
  action       TEXT NOT NULL,
  detail       TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | denied
  decided_by   TEXT,
  decided_at   REAL,
  created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_approvals_status ON approvals(status, created_at);

CREATE TABLE IF NOT EXISTS calls (
  id           TEXT PRIMARY KEY,
  employee_id  TEXT,
  verified     INTEGER NOT NULL DEFAULT 0,
  outcome      TEXT,
  transcript   TEXT NOT NULL DEFAULT '[]',
  started_at   REAL NOT NULL,
  ended_at     REAL
);

CREATE TABLE IF NOT EXISTS reviews (
  call_id      TEXT PRIMARY KEY,
  verdict      TEXT NOT NULL,
  reviewer     TEXT NOT NULL,          -- which provider/model actually judged it
  independent  INTEGER NOT NULL,       -- 0 when it fell back to the call provider
  audit_flag   INTEGER NOT NULL DEFAULT 0,
  created_at   REAL NOT NULL
);
"""


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    P.data.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(P.db, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


def _rows(cur) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


# --- tickets ---------------------------------------------------------------

def create_ticket(employee_id: str, path: str, summary: str, urgency: str,
                  steps_tried: list[str], call_id: str,
                  pending_sync: bool = False) -> str:
    tid = "INC" + uuid.uuid4().hex[:8].upper()
    with conn() as c:
        c.execute(
            "INSERT INTO tickets (id, employee_id, path, summary, urgency, steps_tried,"
            " call_id, created_at, pending_sync) VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, employee_id, path, summary, urgency, json.dumps(steps_tried),
             call_id, time.time(), int(pending_sync)),
        )
    return tid


def ticket_history(employee_id: str, limit: int = 5) -> list[dict[str, Any]]:
    with conn() as c:
        return _rows(c.execute(
            "SELECT id, path, summary, status, created_at FROM tickets"
            " WHERE employee_id = ? ORDER BY created_at DESC LIMIT ?",
            (employee_id, limit)))


def unsynced_tickets() -> list[dict[str, Any]]:
    with conn() as c:
        return _rows(c.execute("SELECT * FROM tickets WHERE pending_sync = 1"))


def mark_synced(ticket_id: str) -> None:
    with conn() as c:
        c.execute("UPDATE tickets SET pending_sync = 0 WHERE id = ?", (ticket_id,))


# --- approvals -------------------------------------------------------------

def request_approval(call_id: str, employee_id: str, action: str, detail: str) -> str:
    aid = "APR" + uuid.uuid4().hex[:8].upper()
    with conn() as c:
        c.execute(
            "INSERT INTO approvals (id, call_id, employee_id, action, detail,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (aid, call_id, employee_id, action, detail, time.time()),
        )
    return aid


def approval(approval_id: str) -> dict[str, Any] | None:
    with conn() as c:
        r = c.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return dict(r) if r else None


def pending_approvals() -> list[dict[str, Any]]:
    with conn() as c:
        return _rows(c.execute(
            "SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at"))


def decide_approval(approval_id: str, approved: bool, by: str) -> bool:
    """Returns False if the approval was already decided - decisions are not revisited."""
    with conn() as c:
        cur = c.execute(
            "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?"
            " WHERE id = ? AND status = 'pending'",
            ("approved" if approved else "denied", by, time.time(), approval_id),
        )
        return cur.rowcount == 1


# --- calls & reviews -------------------------------------------------------

def start_call(call_id: str) -> None:
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO calls (id, started_at) VALUES (?,?)",
                  (call_id, time.time()))


def end_call(call_id: str, employee_id: str | None, verified: bool,
             outcome: str, transcript: list[dict[str, str]]) -> None:
    with conn() as c:
        c.execute(
            "UPDATE calls SET employee_id=?, verified=?, outcome=?, transcript=?,"
            " ended_at=? WHERE id=?",
            (employee_id, int(verified), outcome, json.dumps(transcript),
             time.time(), call_id),
        )


def get_call(call_id: str) -> dict[str, Any] | None:
    with conn() as c:
        r = c.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    return dict(r) if r else None


def save_review(call_id: str, verdict: dict[str, Any], reviewer: str,
                independent: bool, audit_flag: bool) -> None:
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO reviews (call_id, verdict, reviewer, independent,"
            " audit_flag, created_at) VALUES (?,?,?,?,?,?)",
            (call_id, json.dumps(verdict), reviewer, int(independent),
             int(audit_flag), time.time()),
        )


def reviews(limit: int = 50) -> list[dict[str, Any]]:
    with conn() as c:
        return _rows(c.execute(
            "SELECT * FROM reviews ORDER BY created_at DESC LIMIT ?", (limit,)))
