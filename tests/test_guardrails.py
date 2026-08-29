"""The guardrails, tested where they actually live.

Every test here asserts on a code path, not on a model's willingness to follow an
instruction. That is the whole point of the design: a prompt can be argued with.
None of these tests calls a model, so they run in CI with no key and no spend.
"""
from __future__ import annotations

import pytest

from switchboard.models import Risk
from switchboard.tools import base
from switchboard.tools.base import Failure


# --- the registry knows what changes things ---------------------------------

def test_writes_are_declared_not_assumed():
    assert base.writes() == {"ticket.create", "privileged.request"}
    assert base.requires_admin("privileged.request")
    assert not base.requires_admin("ticket.create")
    assert not base.requires_admin("kb.search")


def test_every_read_tool_is_marked_none_risk():
    for name in ("directory.lookup", "directory.verify", "kb.search", "ticket.history"):
        assert base.REGISTRY[name].risk is Risk.NONE, name


# --- privileged actions ------------------------------------------------------

def test_privileged_refuses_unverified_caller():
    """The single most important line in the codebase: a caller who talked their way
    past the script still cannot reach a credential reset."""
    r = base.call("privileged.request", call_id="c1", employee_id="E1042",
                  action="password.reset", reason="locked out",
                  identity_verified=False)
    assert not r.ok and r.failure is Failure.DENIED


def test_privileged_refuses_unknown_action():
    r = base.call("privileged.request", call_id="c1", employee_id="E1042",
                  action="delete.everything", reason="why not",
                  identity_verified=True)
    assert not r.ok and r.failure is Failure.DENIED


def test_privileged_refuses_unlock_on_suspended_account():
    """A suspended account is an HR hold. Unlocking it would quietly undo an
    offboarding decision, so it is refused rather than queued for an admin."""
    r = base.call("privileged.request", call_id="c1", employee_id="E5501",
                  action="account.unlock", reason="cannot sign in",
                  identity_verified=True)
    assert not r.ok and r.failure is Failure.DENIED
    assert "hr" in r.message.lower()


def test_privileged_queues_rather_than_acts():
    r = base.call("privileged.request", call_id="c1", employee_id="E4088",
                  action="account.unlock", reason="locked after failed sign-ins",
                  identity_verified=True)
    assert r.ok and r.value["status"] == "pending"
    # There is no execute path at all - the module cannot perform the action.
    from switchboard.tools import privileged
    assert not hasattr(privileged, "execute")
    assert not hasattr(privileged, "perform")


def test_approval_cannot_be_decided_twice():
    from switchboard.memory import store
    aid = store.request_approval("c9", "E4088", "mfa.reset", "new phone")
    assert store.decide_approval(aid, True, "admin")
    assert not store.decide_approval(aid, False, "someone-else")


# --- tickets -----------------------------------------------------------------

def test_ticket_refuses_without_caller_confirmation():
    r = base.call("ticket.create", employee_id="E1042", path="hardware",
                  summary="laptop is dead", urgency="normal", call_id="c1",
                  caller_confirmed=False)
    assert not r.ok and r.failure is Failure.DENIED


def test_ticket_degrades_when_backend_is_down(monkeypatch):
    """The caller still gets a reference. This is the DOWN path from the framework,
    proven rather than described."""
    monkeypatch.setenv("ITSM_BACKEND", "down")
    r = base.call("ticket.create", employee_id="E1042", path="hardware",
                  summary="laptop is dead", urgency="normal", call_id="c1",
                  caller_confirmed=True)
    assert r.ok and r.value["degraded"]
    assert r.value["ticket_id"] in r.value["tell_caller"]

    from switchboard.memory import store
    assert any(t["id"] == r.value["ticket_id"] for t in store.unsynced_tickets())

    monkeypatch.setenv("ITSM_BACKEND", "up")
    from switchboard.tools import itsm
    assert r.value["ticket_id"] in itsm.reconcile()


# --- identity ----------------------------------------------------------------

def test_verify_refuses_to_be_a_password_oracle():
    for secret in ("hunter2 is my password", "my otp is 448211"):
        r = base.call("directory.verify", employee_id="E1042", answer=secret)
        assert not r.ok and r.failure is Failure.DENIED, secret


def test_verify_matches_either_factor_and_rejects_wrong_ones():
    ok_phone = base.call("directory.verify", employee_id="E1042", answer="8813")
    ok_city = base.call("directory.verify", employee_id="E1042", answer="London")
    bad = base.call("directory.verify", employee_id="E1042", answer="9999")
    assert ok_phone.value["matched"] and ok_city.value["matched"]
    assert not bad.value["matched"]
    # A failed check must not leak whose account it is.
    assert bad.value["name"] is None


def test_lookup_never_returns_the_expected_answers():
    r = base.call("directory.lookup", employee_id="E1042")
    assert r.ok
    blob = str(r.value).lower()
    assert "8813" not in blob and "london" not in blob


# --- knowledge base ----------------------------------------------------------

IN_SCOPE = [
    ("vpn keeps dropping on wifi", "NET-014"),
    ("vpn will not connect at all", "NET-021"),
    ("i am locked out of my account", "ACC-002"),
    ("printer queues but nothing prints", "PRT-003"),
    ("my laptop is really slow", "HW-012"),
    ("laptop will not charge", "HW-005"),
    ("my password expired", "ACC-011"),
]
OUT_OF_SCOPE = [
    "my monitor has a pink tint on the left third",
    "the coffee machine is broken",
    "my chair squeaks",
    "can you order me a new desk",
    "what is the wifi password for guests",
]


@pytest.mark.parametrize("query,expected", IN_SCOPE)
def test_kb_finds_the_right_runbook(query, expected):
    r = base.call("kb.search", query=query)
    assert r.ok, f"{query!r} found nothing"
    assert r.value[0]["id"] == expected


@pytest.mark.parametrize("query", OUT_OF_SCOPE)
def test_kb_returns_empty_rather_than_a_plausible_wrong_fix(query):
    """The behaviour this protects: an agent that improvises a fix from a weak match
    is more expensive than one that says it does not know."""
    r = base.call("kb.search", query=query)
    assert not r.ok and r.failure is Failure.EMPTY, f"{query!r} matched something"


def test_kb_threshold_keeps_a_real_margin():
    """Pins the separation measured when the scorer was built. If a runbook edit
    narrows this, the suite says so before the agent starts guessing on calls."""
    from switchboard.config import KB_MIN_SCORE
    from switchboard.tools import kb

    def top(q):
        return max(kb._score(q, d) for d in kb._runbooks())

    worst_hit = min(top(q) for q, _ in IN_SCOPE)
    best_miss = max(top(q) for q in OUT_OF_SCOPE)
    assert best_miss < KB_MIN_SCORE <= worst_hit
    assert worst_hit - best_miss >= 3.0, (
        f"margin collapsed: hits bottom out at {worst_hit:.1f}, "
        f"misses top out at {best_miss:.1f}")


# --- failure handling --------------------------------------------------------

def test_transient_failures_retry_and_others_do_not():
    calls = {"n": 0}

    @base.tool("test.flaky", Risk.NONE, "test only", retries=2)
    def flaky(kind: Failure):
        calls["n"] += 1
        raise base.ToolError(kind, "boom")

    calls["n"] = 0
    base.call("test.flaky", kind=Failure.TRANSIENT)
    assert calls["n"] == 3, "transient should be attempted 1 + 2 retries"

    for kind in (Failure.EMPTY, Failure.DENIED, Failure.BAD_INPUT):
        calls["n"] = 0
        base.call("test.flaky", kind=kind)
        assert calls["n"] == 1, f"{kind} must never be retried"


def test_unknown_tool_is_bad_input_not_an_exception():
    r = base.call("does.not.exist")
    assert not r.ok and r.failure is Failure.BAD_INPUT


def test_wrong_arguments_are_reported_not_raised():
    r = base.call("kb.search", nonsense=1)
    assert not r.ok and r.failure is Failure.BAD_INPUT


def test_approval_is_idempotent_within_a_call():
    """A re-entered node must not ask the operator the same question twice."""
    from switchboard.memory import store
    a1 = store.request_approval("call-dup", "E4088", "account.unlock", "locked out")
    a2 = store.request_approval("call-dup", "E4088", "account.unlock", "locked out")
    assert a1 == a2

    pending = [a for a in store.pending_approvals() if a["call_id"] == "call-dup"]
    assert len(pending) == 1

    # A different action in the same call is a different question.
    a3 = store.request_approval("call-dup", "E4088", "mfa.reset", "new phone")
    assert a3 != a1

    # Once decided, a fresh request is legitimate - the caller may ring back with more.
    assert store.decide_approval(a1, False, "admin")
    a4 = store.request_approval("call-dup", "E4088", "account.unlock", "now verified")
    assert a4 != a1
