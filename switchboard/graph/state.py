"""Call state — the three tiers from the framework, made concrete.

Turn-scoped fields are overwritten each turn. Call-scoped fields accumulate and are
checkpointed to SQLite after every node, so a dropped call resumes with the identity
already verified and the steps already tried instead of starting the interrogation
again. Caller-scoped memory does not live here at all: it is in `memory.store`, keyed
by employee, and is read into `history` at verification time.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

Stage = Literal["intake", "verify", "triage", "resolve", "escalate", "closed"]


class CallState(TypedDict, total=False):
    # --- identity of the call itself
    call_id: str
    stage: Stage
    turn: int

    # --- turn-scoped: replaced every turn
    utterance: str
    asr_confidence: float
    reply: str                      # what the agent says back this turn

    # --- call-scoped: accumulated
    transcript: Annotated[list[dict[str, str]], operator.add]
    employee_id: str
    employee_name: str
    verified: bool
    # WHICH employee was proven. `verified` alone is not enough: a caller who proved
    # they were one person could then name a different employee id and inherit the
    # verified flag, and every downstream check that asks "is this caller verified"
    # would say yes about the wrong person.
    verified_id: str
    verify_attempts: int
    unclear_turns: int

    issue: str                      # the problem in the caller's words
    triage: dict[str, Any]          # a Triage, as a dict for checkpoint friendliness
    runbook_id: str
    steps_taken: Annotated[list[str], operator.add]
    history: list[dict[str, Any]]   # caller-tier memory, read at verification

    # --- outcomes
    outcome: str                    # resolved | escalated | transferred | abandoned
    ticket_id: str
    approval_id: str
    pending_confirm: str            # what we asked the caller to confirm, if anything
    handoff_reason: str             # why self-serve ended; the caller is told this

    # --- failure bookkeeping, surfaced to the reviewer and the eval
    degraded: bool                  # a dependency was down and we took the local path
    errors: Annotated[list[dict[str, str]], operator.add]
    ended: bool


def new_state(call_id: str) -> CallState:
    return CallState(
        call_id=call_id, stage="intake", turn=0, transcript=[], verified=False,
        verify_attempts=0, unclear_turns=0, steps_taken=[], errors=[],
        degraded=False, ended=False, history=[],
    )
