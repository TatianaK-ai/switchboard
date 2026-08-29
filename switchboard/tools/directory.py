"""Employee directory: lookup and identity verification. Both reads."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from ..config import P
from ..models import Risk
from .base import Failure, ToolError, maybe_fault, tool


@lru_cache(maxsize=1)
def _directory() -> dict[str, dict[str, Any]]:
    data = json.loads(P.employees.read_text(encoding="utf-8"))
    return {e["id"].upper(): e for e in data["employees"]}


@tool("directory.lookup", Risk.NONE,
      "Look up an employee by id. Returns name, department, manager, MFA enrolment and "
      "account status. Does not prove who is on the phone - use directory.verify for "
      "that. Never returns anything about a different employee.")
def lookup(employee_id: str) -> dict[str, Any]:
    maybe_fault("directory.lookup")
    emp = _directory().get((employee_id or "").strip().upper())
    if not emp:
        raise ToolError(Failure.EMPTY, f"no employee with id {employee_id!r}")
    return {
        "id": emp["id"], "name": emp["name"], "dept": emp["dept"],
        "manager": emp["manager"], "mfa_enrolled": emp["mfa_enrolled"],
        "status": emp["status"],
        # The expected answers are never returned - the agent checks, it does not read.
        "verify_prompt": "the last four digits of your desk phone, or your office city",
    }


@tool("directory.verify", Risk.NONE,
      "Check a detail the caller supplied against the directory. Accepts the last four "
      "digits of their desk phone OR their office city. Returns whether it matched. "
      "Never accepts a password, PIN or one-time code - passing one is an error.")
def verify(employee_id: str, answer: str) -> dict[str, Any]:
    maybe_fault("directory.verify")
    emp = _directory().get((employee_id or "").strip().upper())
    if not emp:
        raise ToolError(Failure.EMPTY, f"no employee with id {employee_id!r}")

    given = (answer or "").strip().lower()
    if not given:
        raise ToolError(Failure.BAD_INPUT, "empty answer")

    # Refuse to be used as a password oracle. If something that looks like a secret is
    # passed in, that is a bug or an attack; either way it must not be compared.
    if len(given) > 24 or any(w in given for w in ("password", "passcode", "otp")):
        raise ToolError(Failure.DENIED,
                        "verification takes a phone suffix or office city, never a secret")

    v = emp["verify"]
    matched = given == v["last4_phone"].lower() or given == v["office"].lower()
    return {"matched": matched, "employee_id": emp["id"],
            "status": emp["status"], "name": emp["name"] if matched else None}
