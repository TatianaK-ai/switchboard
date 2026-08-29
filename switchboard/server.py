"""HTTP surface: the voice webhook ElevenLabs calls, and the operator console.

    python -m switchboard.server        # http://127.0.0.1:8080

ElevenLabs Conversational AI runs the voice loop and calls `POST /voice/turn` once per
caller utterance, saying back exactly what it returns. The dialogue policy therefore
lives in the LangGraph service, not in a prompt in someone's dashboard - which is what
makes it testable, and what makes the guardrails hold when the caller gets creative.

The console at `/` is the other half of human-in-the-loop: privileged actions queue
there and a real person releases them.
"""
from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .config import WEBHOOK_SECRET
from .graph.build import snapshot, turn
from .memory import store
from .review import containment, review_call
from .tools import itsm

app = FastAPI(title="Switchboard", docs_url="/api/docs")


def _authed(presented: str | None) -> bool:
    """Constant-time compare. When no secret is configured the server binds loopback
    only (see __main__ below) and accepts unauthenticated calls for local development."""
    if not WEBHOOK_SECRET:
        return True
    return bool(presented) and hmac.compare_digest(presented, WEBHOOK_SECRET)


class TurnIn(BaseModel):
    # Optional, and deliberately not trusted. A voice model asked to supply a
    # conversation id will cheerfully invent one - the first real call arrived with
    # call_id="1" - and every caller would then share a single graph thread, merging
    # one person's verified identity into another's call. The id is taken from the
    # platform-injected header instead; this field is only a local-testing fallback.
    call_id: str = Field(default="", max_length=128)
    utterance: str = Field(default="", max_length=2000)
    asr_confidence: float = Field(default=1.0, ge=0.0, le=1.0)


@app.post("/voice/turn")
def voice_turn(body: TurnIn,
               x_switchboard_secret: str | None = Header(None),
               x_conversation_id: str | None = Header(None)) -> Any:
    """One caller utterance in, one line to speak out."""
    if not _authed(x_switchboard_secret):
        raise HTTPException(401, "bad or missing secret")

    call_id = (x_conversation_id or "").strip() or body.call_id.strip()
    if not call_id:
        raise HTTPException(400, "no conversation id: expected the X-Conversation-Id "
                                 "header (set by the platform) or call_id in the body")
    # A model-invented id like "1" or "12345" is a collision waiting to happen. Anything
    # that short cannot be a real conversation id, so refuse rather than silently share
    # a thread between callers.
    if not x_conversation_id and len(call_id) < 6:
        raise HTTPException(400, f"call_id {call_id!r} looks invented, not a real "
                                 "conversation id")

    out = turn(call_id, body.utterance, body.asr_confidence)
    # Only what the voice layer needs. Ticket ids and approval ids are included because
    # the agent reads them aloud; internal state is not.
    return {
        "reply": out["reply"],
        "end_call": out["ended"],
        "ticket_id": out.get("ticket_id"),
        "approval_id": out.get("approval_id"),
    }


@app.post("/voice/ended")
def voice_ended(payload: dict, x_switchboard_secret: str | None = Header(None)) -> Any:
    """ElevenLabs post-call webhook. Kicks off the independent review."""
    if not _authed(x_switchboard_secret):
        raise HTTPException(401, "bad or missing secret")
    call_id = payload.get("call_id") or payload.get("conversation_id")
    if not call_id:
        raise HTTPException(400, "call_id is required")
    verdict = review_call(call_id)
    return {"reviewed": bool(verdict), "verdict": verdict}


@app.get("/api/calls/{call_id}")
def call_state(call_id: str) -> Any:
    st = snapshot(call_id)
    if not st:
        raise HTTPException(404, "unknown call")
    return {k: v for k, v in st.items() if k != "utterance"}


# --- operator console ------------------------------------------------------

@app.get("/api/approvals")
def approvals() -> Any:
    return {"pending": store.pending_approvals()}


class Decision(BaseModel):
    approved: bool
    by: str = Field(default="operator", max_length=64)


@app.post("/api/approvals/{approval_id}")
def decide(approval_id: str, body: Decision) -> Any:
    if not store.approval(approval_id):
        raise HTTPException(404, "unknown approval")
    if not store.decide_approval(approval_id, body.approved, body.by):
        # Decisions are not revisited: a second click must not flip a release.
        raise HTTPException(409, "already decided")
    return {"ok": True, "status": "approved" if body.approved else "denied"}


@app.get("/api/reviews")
def reviews() -> Any:
    import json
    rows = []
    for r in store.reviews(limit=25):
        rows.append({**r, "verdict": json.loads(r["verdict"])})
    return {"metrics": containment(), "reviews": rows}


@app.post("/api/reconcile")
def reconcile() -> Any:
    """Push tickets that were written locally while the ITSM backend was down."""
    return {"pushed": itsm.reconcile()}


@app.get("/health")
def health() -> Any:
    return {"ok": True, "auth": bool(WEBHOOK_SECRET)}


CONSOLE = """<!doctype html><html><head><meta charset="utf-8">
<title>Switchboard — operator console</title><style>
:root{--bg:#0f1115;--panel:#171a20;--line:#262b34;--ink:#e7eaee;--mut:#8b929c;
--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--acc:#7fa9e8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.55 ui-sans-serif,system-ui,Segoe UI,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mut);margin:0 0 24px}
h2{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--mut);
margin:28px 0 10px;border-bottom:1px solid var(--line);padding-bottom:8px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;
padding:14px 16px;margin-bottom:10px}
.row{display:flex;gap:14px;align-items:flex-start;justify-content:space-between}
.act{font-family:ui-monospace,Consolas,monospace;color:var(--warn)}
.mut{color:var(--mut);font-size:13px}
button{background:var(--acc);color:#0f1115;border:0;border-radius:4px;padding:7px 14px;
font-weight:600;cursor:pointer;font-size:13px}
button.deny{background:transparent;color:var(--bad);border:1px solid var(--bad)}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.m{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:14px}
.m b{display:block;font-size:24px;font-variant-numeric:tabular-nums}
.m span{color:var(--mut);font-size:11px;letter-spacing:.09em;text-transform:uppercase}
.flag{color:var(--bad);font-weight:600}.pass{color:var(--ok)}
.empty{color:var(--mut);font-style:italic}
</style></head><body><div class="wrap">
<h1>Switchboard — operator console</h1>
<p class="sub">Privileged actions the agent requested but may not perform. Nothing here
happens until a person releases it.</p>
<h2>Pending approvals</h2><div id="approvals"></div>
<h2>Call quality <span class="mut">(independent post-call review)</span></h2>
<div class="metrics" id="metrics"></div><div id="reviews"></div>
</div><script>
async function load(){
  const a = await (await fetch('/api/approvals')).json();
  document.getElementById('approvals').innerHTML = a.pending.length ? a.pending.map(p=>`
    <div class="card"><div class="row"><div>
      <div class="act">${p.action}</div>
      <div>${p.detail}</div>
      <div class="mut">${p.employee_id} · call ${p.call_id} · ${p.id}</div>
    </div><div style="display:flex;gap:8px">
      <button onclick="decide('${p.id}',true)">Approve</button>
      <button class="deny" onclick="decide('${p.id}',false)">Deny</button>
    </div></div></div>`).join('') : '<p class="empty">Nothing waiting.</p>';

  const r = await (await fetch('/api/reviews')).json();
  const m = r.metrics;
  document.getElementById('metrics').innerHTML = m.calls ? `
    <div class="m"><b>${(m.contained_and_correct*100).toFixed(0)}%</b><span>Contained &amp; correct</span></div>
    <div class="m"><b class="${m.false_containment>0?'flag':'pass'}">${(m.false_containment*100).toFixed(0)}%</b><span>False containment</span></div>
    <div class="m"><b>${(m.process_clean*100).toFixed(0)}%</b><span>Process clean</span></div>
    <div class="m"><b>${m.flagged_for_audit}</b><span>Flagged for audit</span></div>
    <div class="m"><b>${m.calls}</b><span>Calls reviewed</span></div>
    <div class="m"><b style="font-size:14px">${m.independently_reviewed}</b><span>Independently reviewed</span></div>`
    : '<p class="empty">No reviewed calls yet.</p>';

  document.getElementById('reviews').innerHTML = r.reviews.map(x=>`
    <div class="card"><div class="row"><div>
      <div>${x.verdict.reasoning}</div>
      <div class="mut">${x.call_id} · quality ${x.verdict.quality}/5 ·
        ${x.verdict.resolved?'resolved':'not resolved'} ·
        ${x.verdict.process_followed?'<span class="pass">process ok</span>':'<span class="flag">process breach</span>'}
        ${x.audit_flag?' · <span class="flag">AUDIT</span>':''}</div>
      ${x.verdict.policy_violations.length?`<div class="flag">${x.verdict.policy_violations.join('; ')}</div>`:''}
      <div class="mut">reviewer: ${x.reviewer}${x.independent?'':' — NOT INDEPENDENT'}</div>
    </div></div></div>`).join('');
}
async function decide(id,ok){
  await fetch('/api/approvals/'+id,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({approved:ok,by:'console'})});
  load();
}
load(); setInterval(load, 4000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def console() -> str:
    return CONSOLE


def main() -> None:
    import uvicorn
    store.init()
    pushed = itsm.reconcile()
    if pushed:
        print(f"reconciled {len(pushed)} ticket(s) held while the backend was down")

    # Without a shared secret the write endpoints are open, so refuse to listen off-box.
    host = "0.0.0.0" if WEBHOOK_SECRET else "127.0.0.1"
    if not WEBHOOK_SECRET:
        print("! WEBHOOK_SECRET is not set — binding 127.0.0.1 only.")
        print("! Set it before exposing this to ElevenLabs.")
    port = int(os.getenv("PORT", "8080"))
    print(f"Switchboard console on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
