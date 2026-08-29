"""Ticketing, and history.

`ticket.create` is a write, but the human in its loop is the caller: the agent states
what it is filing and needs a spoken yes. That is a deliberate departure from
"every write needs an admin" - routing ticket creation through an approval queue would
mean nobody ever gets a ticket number before hanging up, which defeats the point of
the call. Privileged writes still go to an admin; see tools/privileged.py.

If the ITSM backend is unreachable the ticket is written locally and marked
pending_sync. The caller still gets an outcome. This is the DOWN path from the
framework, and it is exercised by the eval suite rather than merely described.
"""
from __future__ import annotations

import os
from typing import Any

from ..memory import store
from ..models import Risk
from .base import Failure, ToolError, maybe_fault, tool


def _backend_up() -> bool:
    """Stands in for a real ServiceNow/Jira call. Flipped by the eval harness to prove
    the degraded path works."""
    return os.getenv("ITSM_BACKEND", "up") == "up"


@tool("ticket.history", Risk.NONE,
      "Recent tickets for this employee, newest first. Call it before proposing a fix: "
      "if they reported the same thing recently and it came back, re-walking the same "
      "script wastes the call and the issue should escalate sooner.")
def history(employee_id: str, limit: int = 5) -> list[dict[str, Any]]:
    maybe_fault("ticket.history")
    return store.ticket_history(employee_id, limit)


@tool("ticket.create", Risk.CALLER_CONFIRM,
      "File a support ticket. WRITE - only call this after the caller has heard the "
      "summary and said yes. Include every step already tried so a human does not "
      "repeat them. Returns the ticket reference to read back to the caller.")
def create(employee_id: str, path: str, summary: str, urgency: str,
           steps_tried: list[str] | None = None, call_id: str = "",
           caller_confirmed: bool = False) -> dict[str, Any]:
    maybe_fault("ticket.create")

    # The confirmation gate is here, in code, and not only in the prompt. A model that
    # decides to be helpful and skip the question still cannot file the ticket.
    if not caller_confirmed:
        raise ToolError(Failure.DENIED,
                        "caller has not confirmed - read the summary back and ask first")
    if not (summary or "").strip():
        raise ToolError(Failure.BAD_INPUT, "summary is required")

    degraded = not _backend_up()
    tid = store.create_ticket(
        employee_id=employee_id, path=path, summary=summary, urgency=urgency,
        steps_tried=steps_tried or [], call_id=call_id, pending_sync=degraded,
    )
    return {
        "ticket_id": tid,
        "degraded": degraded,
        # What the agent should actually say, so the degraded case does not leak
        # implementation detail to a caller who does not care.
        "tell_caller": (
            f"Your reference is {tid}."
            if not degraded else
            f"Your reference is {tid}. Our ticket system is having trouble right now, "
            "so it may take a few minutes to appear, but it is recorded."
        ),
    }


def reconcile() -> list[str]:
    """Push locally-held tickets once the backend recovers. Called on server start and
    by the /admin/reconcile endpoint."""
    if not _backend_up():
        return []
    pushed = []
    for t in store.unsynced_tickets():
        store.mark_synced(t["id"])
        pushed.append(t["id"])
    return pushed
