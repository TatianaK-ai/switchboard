"""Employee directory: lookup and identity verification. Both reads."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from ..config import P
from ..models import Risk
from .base import Failure, ToolError, maybe_fault, tool


#: The only two shapes a verification answer may take.
_PHONE_SUFFIX = re.compile(r"\d{4}")
_CITY = re.compile(r"[a-z][a-z .'-]{1,23}")


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

    # Allow-list the SHAPE of an answer rather than deny-listing words that look secret.
    # A blocklist of "password"/"otp" only catches an attacker who labels their guess:
    # `hunter2` and `1234` sailed through it and were compared normally, which is
    # precisely the oracle this is supposed not to be. Exactly four digits, or a plain
    # city name. Anything else is refused before any comparison happens.
    if not (_PHONE_SUFFIX.fullmatch(given) or _CITY.fullmatch(given)):
        raise ToolError(
            Failure.DENIED,
            "verification takes exactly four digits of a desk phone, or an office "
            "city - nothing else is compared")

    v = emp["verify"]
    matched = given == v["last4_phone"].lower() or given == v["office"].lower()

    # Nothing about the account leaks on a failed check. Returning `status` told an
    # unverified caller whether the account was suspended, which is exactly the sort of
    # thing a stranger probing the line is trying to learn.
    if not matched:
        return {"matched": False, "employee_id": emp["id"], "name": None}
    return {"matched": True, "employee_id": emp["id"],
            "status": emp["status"], "name": emp["name"]}
