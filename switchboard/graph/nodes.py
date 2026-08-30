"""The specialist agents, one per node.

Each node does one job, decides one thing, and hands control back to the graph. The
routing between them is ordinary Python in `build.py`, not a model deciding what to do
next: control flow is the part that has to be reliable when someone is on the phone,
and a graph can be tested where a free-running loop can only be hoped about. The model
is used at the edges - understanding what was said, and choosing words to say back.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..config import (ASR_MIN_CONFIDENCE, MAX_TURNS, MAX_UNCLEAR_TURNS,
                      MAX_VERIFY_ATTEMPTS)
from ..llm import extract, speak
from ..memory import store
from ..models import Path_, ResolutionAttempt, Triage
from ..tools import base as tools
from ..tools.base import Failure
from .state import CallState

VOICE = (
    "You are Switchboard, the IT support line for a mid-sized company. You are on a "
    "phone call, so: short sentences, one question at a time, no bullet points, no "
    "markdown, no reading out URLs or codes character by character unless asked. "
    "Never ask for, accept, or repeat a password, PIN or one-time code. If the caller "
    "starts to read one out, stop them. Do not invent fixes."
)


def _convo(state: CallState, extra: str = "") -> str:
    lines = [f"{t['role']}: {t['text']}" for t in state.get("transcript", [])]
    if extra:
        lines.append(extra)
    return "\n".join(lines) or "(no conversation yet)"


def _verified_as(state: CallState, employee_id: str) -> bool:
    """Verification is per person. Proving you are E1042 says nothing about E4088."""
    return bool(state.get("verified")) and         (state.get("verified_id", "") or "").upper() == (employee_id or "").upper()


def _err(state: CallState, tool: str, res: tools.ToolResult) -> dict[str, Any]:
    return {"errors": [{"turn": str(state.get("turn", 0)), "tool": tool,
                        "kind": res.failure.value if res.failure else "unknown",
                        "message": res.message}]}


# --------------------------------------------------------------------------
# ingest — every turn passes through here first

#: Short ways of saying "no, nothing else". Checked literally rather than with a model
#: call: this runs on every turn of a finished call and must be instant and predictable.
_DECLINES = {"no", "nope", "no thanks", "no thank you", "nothing", "that's all",
             "thats all", "that is all", "all good", "im good", "i'm good", "bye",
             "goodbye", "nothing else", "no that's it", "no thats it"}


def ingest(state: CallState) -> dict[str, Any]:
    """Record what was heard, and decide whether we actually heard it."""
    text = (state.get("utterance") or "").strip()
    conf = float(state.get("asr_confidence", 1.0))
    out: dict[str, Any] = {"turn": state.get("turn", 0) + 1,
                           "transcript": [{"role": "caller", "text": text}]}

    # The call is finished but the caller is still talking. Replaying the closing line
    # forever - which is what happened on the first real call - is the worst possible
    # answer: the agent invites more ("anything else?") and then cannot hear it.
    if state.get("ended") and text:
        if text.lower().strip(" .!?") in _DECLINES:
            out.update(stage="closed", ended=True,
                       reply="Thanks for calling. Goodbye.")
            return out
        # Anything substantive starts a fresh request inside the same call. Identity
        # already established is kept; everything about the previous issue is dropped
        # so the new one cannot inherit its ticket or its runbook.
        out.update(stage="intake", ended=False, issue="", triage={}, runbook_id="",
                   ticket_id="", approval_id="", pending_confirm="",
                   handoff_reason="", outcome="", reply="")
        return out

    if conf < ASR_MIN_CONFIDENCE or not text:
        unclear = state.get("unclear_turns", 0) + 1
        out["unclear_turns"] = unclear
        if unclear >= MAX_UNCLEAR_TURNS:
            # Guessing at a half-heard sentence about a locked account is how you end
            # up resetting the wrong person's credentials. Offer a callback instead.
            out.update(stage="escalate", ended=False,
                       handoff_reason="I'm not able to hear you well enough to "
                                      "help safely",
                       issue=state.get("issue") or "line quality too poor to continue",
                       reply="I'm having real trouble hearing you. Rather than guess, "
                             "let me arrange a callback from the service desk.")
            out["outcome"] = "transferred"
        else:
            out["reply"] = "Sorry, the line broke up there. Could you say that again?"
    else:
        out["unclear_turns"] = 0
    return out


# --------------------------------------------------------------------------
# intake

class _Intake(BaseModel):
    employee_id: str = Field(
        default="",
        description="The caller's employee id if they have given one, normalised to "
                    "the form E1234. Empty string if they have not given one yet. Do "
                    "not invent or guess an id from a name.")
    issue: str = Field(
        default="",
        description="The problem in the caller's own words, one sentence. Empty if "
                    "they have not described a problem yet. Keep their phrasing - "
                    "'it keeps kicking me off' is more useful to a human than "
                    "'intermittent disconnection'.")


def intake(state: CallState) -> dict[str, Any]:
    got = extract(
        _Intake,
        VOICE + " Extract only what the caller has actually said. Leave fields empty "
                "rather than filling them in with something plausible.",
        _convo(state),
    )
    emp = got.employee_id or state.get("employee_id", "")
    issue = got.issue or state.get("issue", "")
    out: dict[str, Any] = {"employee_id": emp, "issue": issue}

    # Naming a different employee drops the verified flag. Without this a caller could
    # verify as themselves and then act as a colleague.
    if emp and state.get("verified") and not _verified_as(state, emp):
        out["verified"] = False
        out["verified_id"] = ""

    if emp and issue:
        # Stage advances only after the directory confirms this id exists. Setting it
        # before the lookup meant an unknown id still moved the call to `verify`, which
        # then tried to check an answer against nobody, failed, and escalated - the
        # caller was never asked for their id again.
        res = tools.call("directory.lookup", employee_id=emp)
        if not res.ok:
            out.update(_err(state, "directory.lookup", res))
            if res.failure is Failure.EMPTY:
                out["reply"] = (f"I can't find employee id {emp} in the directory. "
                                "Could you read it back to me?")
                out["employee_id"] = ""
                return out
            # Directory down: we cannot verify, so we cannot do anything privileged.
            # Take the call anyway and file a ticket rather than hanging up on them.
            out.update(stage="escalate", degraded=True,
                       reply="Our directory is not responding, so I can't verify you "
                             "right now. I can still log this for the team.")
            return out
        emp_rec = res.value
        out["employee_name"] = emp_rec["name"]

        # Identity is proven once per call, not once per issue. A caller who was
        # verified, got one thing fixed and then raised a second problem must not be
        # interrogated again - the first real call did exactly that, and it reads as
        # the agent having forgotten who it was talking to.
        if _verified_as(state, emp):
            out["stage"] = "triage"
            out["reply"] = "Right, let me look into that one."
            return out

        out["stage"] = "verify"
        out["reply"] = (
            f"Thanks. To confirm it's you — can I take {emp_rec['verify_prompt']}?"
        )
        return out

    if not emp:
        out["reply"] = ("I can help with that. Can I take your employee id first? "
                        "It starts with an E.") if issue else (
            "IT support, this is Switchboard. What's going on?")
    else:
        out["reply"] = "Thanks. What's the problem you're seeing?"
    return out


# --------------------------------------------------------------------------
# verify

class _Answer(BaseModel):
    answer: str = Field(
        default="",
        description="The verification detail the caller just gave - a four digit phone "
                    "suffix or a city name. Empty if they did not give one. If they "
                    "offered a password or a one-time code instead, return the literal "
                    "string REFUSED.")


def verify(state: CallState) -> dict[str, Any]:
    got = extract(_Answer, VOICE + " Extract only the verification detail.",
                  _convo(state))

    if got.answer == "REFUSED" or not got.answer:
        return {"reply": "I can only ever act on the account of the person I've "
                         "verified, and I don't handle passwords or codes on a "
                         "call. To carry on with your own account, I just need the "
                         "last four digits of your desk phone, or your office city."}

    res = tools.call("directory.verify", employee_id=state["employee_id"],
                     answer=got.answer)
    if not res.ok:
        out = _err(state, "directory.verify", res)
        out["reply"] = ("I don't need that — just your desk phone's last four digits, "
                        "or your office city.") if res.failure is Failure.DENIED else (
            "Something went wrong checking that. Let me log this for the team instead.")
        if res.failure is not Failure.DENIED:
            out.update(stage="escalate", degraded=True)
        return out

    v = res.value
    if v["matched"]:
        hist = tools.call("ticket.history", employee_id=state["employee_id"])
        return {
            "verified": True, "verified_id": v["employee_id"], "stage": "triage",
            "employee_name": v["name"],
            "history": hist.value if hist.ok else [],
            "reply": f"Thanks {v['name'].split()[0]}, that matches. Let me look into it.",
        }

    attempts = state.get("verify_attempts", 0) + 1
    if attempts >= MAX_VERIFY_ATTEMPTS:
        # Two misses ends self-serve. Not a punishment - an unverified caller must not
        # reach a tool that changes anything, and there is nothing useful left to offer.
        return {
            "verify_attempts": attempts, "stage": "escalate", "outcome": "transferred",
            "reply": "That doesn't match what I have either. For security I can't carry "
                     "on without verifying you, so I'm passing this to the service desk "
                     "— they can verify you another way.",
        }
    return {"verify_attempts": attempts,
            "reply": "That doesn't match what I have. Let's try the other one — "
                     "which office are you based in?"}


# --------------------------------------------------------------------------
# triage

def triage(state: CallState) -> dict[str, Any]:
    prior = ""
    if state.get("history"):
        prior = ("Previous tickets for this caller: " +
                 "; ".join(f"{h['summary']} ({h['status']})"
                           for h in state["history"][:3]))
    got = extract(
        Triage,
        VOICE + " Classify the caller's issue for routing. You are not solving it yet.",
        _convo(state, prior),
    )
    return {"stage": "resolve", "triage": got.model_dump(mode="json")}


# --------------------------------------------------------------------------
# resolve

class _Step(BaseModel):
    reply: str = Field(
        description="What to say to the caller next: either the next step to try, "
                    "phrased conversationally for speech, or a question checking "
                    "whether the last step worked. One step at a time - never read out "
                    "the whole runbook.")
    resolution: ResolutionAttempt


def resolve(state: CallState) -> dict[str, Any]:
    tri = state.get("triage") or {}
    query = tri.get("summary") or state.get("issue", "")

    # The caller's own short symptom first: runbook titles are written in symptom
    # language, and triage's summary is written for a human to read in a ticket. Longer
    # paraphrase dilutes a proportional term match, so it is the fallback, not the lead.
    path = tri.get("path", "")
    attempts = [q for q in (state.get("issue", ""), query) if q]
    res = None
    for q in attempts:
        res = tools.call("kb.search", query=q, path=path)
        if res.ok:
            break
    if not res.ok:
        out = _err(state, "kb.search", res)
        # EMPTY is the interesting one: no documented fix. The agent says exactly that
        # and escalates, rather than assembling something that sounds like a fix.
        out.update(stage="escalate",
                   handoff_reason="I don't have a documented fix for that one, so "
                                  "I'm not going to guess.",
                   reply="I don't have a documented fix for that one, so I'm not going "
                         "to guess. Let me get it to someone who can dig in.")
        if res.failure is not Failure.EMPTY:
            out["degraded"] = True
        return out

    hits = res.value
    book = hits[0]

    # A runbook marked privileged cannot be completed by the agent, whatever it says.
    if book["privileged"]:
        return {"stage": "escalate", "runbook_id": book["id"],
                "handoff_reason": "that needs a change only our identity team can "
                                  "make",
                "reply": "That one needs a change only our identity team can make. "
                         "Let me get that moving for you."}

    steps = "\n".join(f"- {ln}" for ln in book["body"].splitlines() if ln.strip())
    done = state.get("steps_taken", [])
    already = ("\n\nSTEPS ALREADY WALKED — do not repeat these, continue from the next "
               "one:\n" + "\n".join(f"- {s}" for s in done)) if done else ""
    got = extract(
        _Step,
        VOICE + "\nWalk the caller through this runbook one step at a time. Do not skip "
                "ahead and do not repeat a step you have already given. If the caller "
                "has said the problem is gone, set resolved and close warmly instead of "
                "giving another step. Never claim it is solved unless they said so in "
                "their own words.\n\nRUNBOOK "
                f"{book['id']} — {book['title']}\n{steps}{already}",
        _convo(state),
    )

    out: dict[str, Any] = {"runbook_id": book["id"], "reply": got.reply}
    if got.resolution.steps_taken:
        out["steps_taken"] = got.resolution.steps_taken
    if got.resolution.resolved:
        out.update(stage="closed", outcome="resolved", ended=True)
    elif state.get("turn", 0) >= MAX_TURNS:
        out.update(stage="escalate",
                   reply="We've been at this a while — let me hand it over rather than "
                         "keep you on the line.")
    return out


# --------------------------------------------------------------------------
# escalate

def escalate(state: CallState) -> dict[str, Any]:
    tri = state.get("triage") or {}
    path = tri.get("path", Path_.UNKNOWN.value)
    urgency = tri.get("urgency", "normal")

    # `.get(key, default)` returns "" when the key exists and is empty, which is
    # exactly the case here after a failed lookup - hence `or`.
    emp = state.get("employee_id") or "UNIDENTIFIED"

    summary = tri.get("summary") or state.get("issue") or "Unspecified issue"
    if not _verified_as(state, emp):
        # A human picking this up must see immediately that nobody proved who
        # was calling, because that changes what they may act on.
        summary = f"[caller not verified] {summary}"

    out: dict[str, Any] = {}

    # Privileged path first: request the action, then still file a ticket so there is a
    # record even if the approval is later denied.
    if tri.get("needs_privileged_action") and _verified_as(state, emp):
        action = {"account": "account.unlock", "software": "access.grant"}.get(
            path, "account.unlock")
        pres = tools.call("privileged.request", call_id=state["call_id"],
                          employee_id=emp, action=action, reason=summary,
                          identity_verified=True)
        if pres.ok:
            out["approval_id"] = pres.value["approval_id"]
        else:
            out.update(_err(state, "privileged.request", pres))

    # The caller confirms the ticket. In the voice flow that yes arrives on the
    # previous turn; the graph asks, then files on the way back through.
    confirmed = state.get("pending_confirm") == "ticket" or state.get("ended")
    if not confirmed:
        why = (state.get("handoff_reason") or "").strip()
        if why:
            why = why[0].upper() + why[1:]
            if not why.endswith((".", "!", "?")):
                why += "."
        lead = f"{why} " if why else ""
        return {**out, "pending_confirm": "ticket",
                "reply": f"{lead}I'll log this as: {summary.rstrip('.')}. Shall I raise that "
                         "ticket so someone can pick it up?"}

    tres = tools.call("ticket.create", employee_id=emp, path=path, summary=summary,
                      urgency=urgency, steps_tried=state.get("steps_taken", []),
                      call_id=state["call_id"], caller_confirmed=True)
    if tres.ok:
        out.update(ticket_id=tres.value["ticket_id"], stage="closed",
                   outcome=state.get("outcome") or "escalated", ended=True,
                   degraded=state.get("degraded", False) or tres.value["degraded"],
                   reply=tres.value["tell_caller"] + " Anything else I can help with?")
    else:
        out.update(_err(state, "ticket.create", tres))
        out.update(stage="closed", outcome="escalated", ended=True,
                   reply="I couldn't file that automatically, so I've flagged it for "
                         "the service desk to pick up manually. Sorry about that.")
    return out


# --------------------------------------------------------------------------
# close

def close(state: CallState) -> dict[str, Any]:
    reply = state.get("reply") or "Glad that's sorted. Anything else?"
    store.end_call(
        call_id=state["call_id"], employee_id=state.get("employee_id"),
        verified=bool(state.get("verified")),
        outcome=state.get("outcome", "abandoned"),
        transcript=state.get("transcript", []) + [{"role": "agent", "text": reply}],
    )
    return {"reply": reply, "ended": True}
