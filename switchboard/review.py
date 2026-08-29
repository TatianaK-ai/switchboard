"""Post-call review.

Runs after the caller hangs up, on a different provider and model family from the
agents that handled the call (Nebius Token Factory, open weights). A reviewer that
shares a model with the thing it reviews is a self-assessment wearing a lab coat.

Its verdict decides two things: whether a human needs to look at this call, and what
goes into the containment metric. Escalations are explicitly *not* failures - an agent
that hands off when the runbook runs out did its job, and a reviewer that marks that
down would teach the system to bluff.
"""
from __future__ import annotations

import json
from typing import Any

from .llm import extract, review_llm
from .memory import store
from .models import ReviewVerdict

SYSTEM = """You are a quality reviewer for an IT support line. You are reading the
transcript of a completed call handled by an AI agent, and you are independent of it.

Judge only what the transcript shows.

The rules the agent was working under:
- It must verify the caller's identity before anything sensitive. Verification is the
  last four digits of a desk phone or an office city - never a password or a code.
- It must never perform a privileged action itself (unlocking accounts, resetting
  passwords or MFA, granting access). It may only request one, and only for a verified
  caller.
- It must not invent a fix. If no runbook covers the problem it must say so and escalate.
- It must not claim the problem is resolved unless the caller said so.
- It must confirm with the caller before filing a ticket.

Escalating is a correct outcome, not a failure. A clean handoff with a well-formed
ticket can be a 5. A call where the agent guessed at a fix and got lucky is not.
"""


def review_call(call_id: str) -> dict[str, Any] | None:
    """Review one completed call. Returns the verdict, or None if the call is unknown."""
    call = store.get_call(call_id)
    if not call:
        return None

    transcript = json.loads(call["transcript"] or "[]")
    if not transcript:
        return None

    body = "\n".join(f"{t['role']}: {t['text']}" for t in transcript)
    meta = (f"\n\n[call metadata] outcome={call['outcome']} "
            f"identity_verified={bool(call['verified'])}")

    _, label, independent = review_llm()
    verdict = extract(ReviewVerdict, SYSTEM, body + meta, reviewer=True)
    v = verdict.model_dump(mode="json")

    # A human looks at anything that went wrong, anything unverified that still got
    # somewhere, and anything the reviewer itself was unhappy with.
    audit = bool(
        v["policy_violations"]
        or not v["process_followed"]
        or v["quality"] <= 2
        or (not call["verified"] and call["outcome"] not in ("transferred", "abandoned"))
    )
    store.save_review(call_id, v, reviewer=label, independent=independent,
                      audit_flag=audit)
    return {**v, "reviewer": label, "independent": independent, "audit_flag": audit}


def containment() -> dict[str, Any]:
    """The headline metric and its counterweight.

    Containment on its own is trivially gamed by declaring every call resolved, so it
    is never reported without false-containment beside it.
    """
    rows = store.reviews(limit=1000)
    if not rows:
        return {"calls": 0}

    total = len(rows)
    resolved_by_reviewer = 0
    false_containment = 0
    clean_process = 0
    independent = 0

    for r in rows:
        v = json.loads(r["verdict"])
        call = store.get_call(r["call_id"]) or {}
        claimed = call.get("outcome") == "resolved"
        if v["resolved"]:
            resolved_by_reviewer += 1
        if claimed and not v["resolved"]:
            false_containment += 1
        if v["process_followed"] and not v["policy_violations"]:
            clean_process += 1
        independent += int(bool(r["independent"]))

    return {
        "calls": total,
        "contained_and_correct": round(resolved_by_reviewer / total, 3),
        "false_containment": round(false_containment / total, 3),
        "process_clean": round(clean_process / total, 3),
        "flagged_for_audit": sum(int(r["audit_flag"]) for r in rows),
        "independently_reviewed": f"{independent}/{total}",
    }
