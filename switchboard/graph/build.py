"""The graph: one caller utterance in, one agent reply out, state checkpointed.

Routing is explicit. `ingest` runs first every turn, then control goes to whichever
specialist the call's stage calls for, then the turn ends. The model never chooses the
next node - if it could, a caller who says the right thing could route themselves
straight past verification into the privileged path.
"""
from __future__ import annotations

import sqlite3
from functools import lru_cache
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from ..config import P
from ..memory import store
from . import nodes
from .state import CallState, new_state


def _route(state: CallState) -> str:
    """Where this turn goes after ingest."""
    if state.get("ended"):
        return "close"
    # ingest already produced a reply (line unclear, or a callback offer) and did not
    # move the call on - end the turn and wait for the caller.
    if state.get("reply") and state.get("stage") not in ("escalate", "closed"):
        if state.get("unclear_turns", 0) > 0:
            return END
    return {
        "intake": "intake", "verify": "verify", "triage": "triage",
        "resolve": "resolve", "escalate": "escalate", "closed": "close",
    }.get(state.get("stage", "intake"), "intake")


def _after(state: CallState) -> str:
    """Some stages fall straight through in the same turn rather than making the caller
    wait for a round trip: verification success goes on to triage, triage goes on to
    the first resolution step, and anything that decided to escalate does so now."""
    stage = state.get("stage")
    if state.get("ended"):
        return "close"
    if stage == "triage":
        return "triage"
    if stage == "resolve":
        return "resolve"
    if stage == "escalate":
        return "escalate"
    return END


@lru_cache(maxsize=1)
def _saver() -> SqliteSaver:
    P.data.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: uvicorn serves turns from a worker pool, and each turn
    # is a separate short transaction.
    return SqliteSaver(sqlite3.connect(P.checkpoints, check_same_thread=False))


@lru_cache(maxsize=1)
def graph():
    g = StateGraph(CallState)
    g.add_node("ingest", nodes.ingest)
    g.add_node("intake", nodes.intake)
    g.add_node("verify", nodes.verify)
    g.add_node("triage", nodes.triage)
    g.add_node("resolve", nodes.resolve)
    g.add_node("escalate", nodes.escalate)
    g.add_node("close", nodes.close)

    g.add_edge(START, "ingest")
    g.add_conditional_edges("ingest", _route, {
        "intake": "intake", "verify": "verify", "triage": "triage",
        "resolve": "resolve", "escalate": "escalate", "close": "close", END: END,
    })
    # Intake and verify may advance the stage; let them continue in the same turn so a
    # verified caller is not left listening to silence before triage happens.
    for n in ("intake", "verify"):
        g.add_conditional_edges(n, _after, {
            "triage": "triage", "resolve": "resolve", "escalate": "escalate",
            "close": "close", END: END,
        })
    g.add_conditional_edges("triage", _after, {
        "resolve": "resolve", "escalate": "escalate", "close": "close", END: END})
    g.add_conditional_edges("resolve", _after, {
        "escalate": "escalate", "close": "close", "resolve": END, END: END})
    g.add_conditional_edges("escalate", _after, {"close": "close", END: END,
                                                 "escalate": END, "resolve": END,
                                                 "triage": END})
    g.add_edge("close", END)
    return g.compile(checkpointer=_saver())


def turn(call_id: str, utterance: str, asr_confidence: float = 1.0) -> dict[str, Any]:
    """Run one turn of a call. Safe to call repeatedly; state is resumed from the
    checkpoint, so a reconnected call picks up mid-conversation."""
    store.init()
    store.start_call(call_id)
    cfg = {"configurable": {"thread_id": call_id}}
    g = graph()

    existing = g.get_state(cfg)
    base = {} if existing.values else new_state(call_id)
    result = g.invoke(
        {**base, "call_id": call_id, "utterance": utterance,
         "asr_confidence": asr_confidence},
        cfg,
    )

    # Write the agent's own reply back into the transcript. Without this the model sees
    # only the caller's half of the conversation on the next turn and cheerfully
    # repeats the step it just gave - which is exactly what it did before this existed.
    reply = result.get("reply", "")
    if reply:
        g.update_state(cfg, {"transcript": [{"role": "agent", "text": reply}]})

    return {
        "reply": result.get("reply", ""),
        "stage": result.get("stage"),
        "ended": bool(result.get("ended")),
        "outcome": result.get("outcome"),
        "ticket_id": result.get("ticket_id"),
        "approval_id": result.get("approval_id"),
        "verified": bool(result.get("verified")),
        "degraded": bool(result.get("degraded")),
        "errors": result.get("errors", []),
    }


def snapshot(call_id: str) -> dict[str, Any]:
    st = graph().get_state({"configurable": {"thread_id": call_id}})
    return dict(st.values) if st.values else {}
