"""Privileged actions — the ones the agent is never allowed to perform.

There is no `execute` here on purpose. The only thing this module can do is *request*
an action and hand it to a human. Even a fully compromised prompt cannot reach a
credential reset through this file, because the code to do it does not exist in the
agent's process.
"""
from __future__ import annotations

from typing import Any

from ..memory import store
from ..models import Risk
from .base import Failure, ToolError, maybe_fault, tool

#: Actions a human administrator may release. Anything not in this set is refused
#: outright rather than queued - an unknown privileged action is not a thing to ask
#: a busy admin to adjudicate at 2am.
ALLOWED = {
    "account.unlock": "Unlock an account locked by failed sign-ins",
    "mfa.reset": "Clear MFA enrolment so the employee can re-enrol on a new device",
    "password.reset": "Send a password reset link to the registered address",
    "access.grant": "Grant access to an application, subject to manager approval",
}


@tool("privileged.request", Risk.ADMIN_APPROVAL,
      "Request a privileged action that only a human administrator may perform: "
      "account.unlock, mfa.reset, password.reset, access.grant. WRITE - this does not "
      "perform the action, it queues it for a person. Identity must be verified first. "
      "Tell the caller they are waiting on a human.")
def request(call_id: str, employee_id: str, action: str, reason: str,
            identity_verified: bool = False) -> dict[str, Any]:
    maybe_fault("privileged.request")

    if action not in ALLOWED:
        raise ToolError(Failure.DENIED,
                        f"{action!r} is not an action this system can request. "
                        f"Allowed: {', '.join(sorted(ALLOWED))}")

    # Verification is enforced here, not merely instructed. This is the single most
    # important line in the codebase: it is what stops a caller who talked their way
    # past the script from reaching a credential reset.
    if not identity_verified:
        raise ToolError(Failure.DENIED,
                        "identity is not verified - a privileged action cannot be "
                        "requested for an unverified caller")
    if not (reason or "").strip():
        raise ToolError(Failure.BAD_INPUT, "a reason is required for the approver")

    emp = None
    try:
        from .directory import lookup
        emp = lookup(employee_id=employee_id)
    except ToolError:
        pass

    # A suspended account is an HR hold, not a lockout. IT unlocking it would quietly
    # undo an offboarding decision, so it is refused rather than queued.
    if emp and emp.get("status") == "suspended" and action in {"account.unlock",
                                                               "password.reset"}:
        raise ToolError(Failure.DENIED,
                        "account is suspended, which is an HR hold and not an IT "
                        "lockout - route to HR Ops, do not request an unlock")

    approval_id = store.request_approval(
        call_id=call_id, employee_id=employee_id, action=action,
        detail=f"{ALLOWED[action]} — {reason.strip()}",
    )
    return {
        "approval_id": approval_id,
        "status": "pending",
        "tell_caller": (
            "I've sent that to our identity team for approval — I'm not able to make "
            "that change myself. You'll get an email once it's released."
        ),
    }


def status(approval_id: str) -> str:
    row = store.approval(approval_id)
    return row["status"] if row else "unknown"
