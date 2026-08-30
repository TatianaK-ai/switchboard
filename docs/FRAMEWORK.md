# Switchboard — the agent framework

A multi-agent IT support voice agent. Week 3, Mastering Agentic AI (Aug 2026 cohort).

---

## The one-liner

> My agent helps **an employee with a broken laptop, a locked account or a dead VPN**
> resolve it **on a phone call**, replacing **the 14-minute hold and the ticket that
> sits in a queue for a day and a half**. It verifies who is calling, finds the
> documented fix, and walks them through it on its own using **six tools**, hands off
> to a human **the moment a fix needs a privileged write, identity cannot be proven,
> or the runbook does not cover the problem**, and I'll know it works when **a caller
> resolves a common issue in under four minutes, and every call the agent handled
> alone passes an independent post-call review at least 8 times out of 10**.

### Against the three rules

**Task completion, not single-shot accuracy.** The unit of success is a *call*, not a
turn. A call succeeds when the caller hangs up either fixed or correctly handed to a
human with a ticket that a human can act on without listening to the recording. The
eval scores whole transcripts, not individual replies.

**State is the hard part.** Three tiers, deliberately separated:

| Tier | Holds | Lives in | Lifetime |
| --- | --- | --- | --- |
| Turn | Current utterance, ASR confidence | In-memory graph state | One turn |
| Call | Verified identity, issue, path taken, steps tried, pending approvals | LangGraph `SqliteSaver` checkpoint keyed by `call_id` | The call, then archived |
| Caller | Previous tickets, recurring issues, prior resolutions | `switchboard.db`, keyed by employee id | Indefinite |

The caller tier is what makes the third call about the same VPN worth handling
differently from the first. It is also the only tier that survives a process restart,
which is the actual test of whether persistence works.

**Write actions deserve a human.** Every tool is classified `read` or `write` in code,
not by convention — see the registry in `tools/base.py`. Reads run autonomously. Writes split in
two, because collapsing them into one rule produces either a useless agent or a
dangerous one:

- **Caller-confirmed writes** — creating a ticket. The human in the loop is the caller:
  the agent states what it is about to file and needs a spoken yes. Reversible, logged.
- **Admin-approved writes** — password reset, MFA re-enrolment, group membership,
  account unlock. The agent can only ever *request* these: `privileged.request` queues
  the action and returns, and the caller is told a person has to release it. A human IT
  admin decides in the operator console.

  **What this is not:** the graph is not suspended while it waits. The call completes,
  a ticket is filed, and the approval outlives the call as a row a human acts on later.
  A LangGraph `interrupt` would hold the graph open mid-call, which is wrong for a phone
  line — the caller would be listening to silence until an admin happened to look at a
  queue. The guarantee here is not that the call blocks; it is that **the action cannot
  happen without a person**, because the code that performs it does not exist in the
  agent's process. That is a code path, not an instruction, and no prompt can talk the
  agent past it.

---

## The framework

| Field | |
| --- | --- |
| **Agent goal** | Take an employee's IT support call from "my laptop won't connect" to either a verified fix or a well-formed ticket in a human's queue, without the caller repeating themselves. |
| **Where do people use it?** | A phone call. ElevenLabs Conversational AI runs the voice loop; every decision is made by a LangGraph service behind it. A text CLI drives the identical graph for development and for the demo's failure cases. |
| **What steps does it take, in order?** | 1. **Intake** — greet, capture the problem in the caller's words, get employee id. 2. **Verify** — confirm identity against the directory; two failures ends the self-serve path. 3. **Triage** — classify into a support path and decide self-serve vs. escalate. 4. **Resolve** — search runbooks, walk the caller through steps, confirm it worked. 5. **Escalate** — when resolution fails or a privileged write is needed: file a ticket, request approval, set expectations. 6. **Review** — after hang-up, a separate agent scores the transcript for resolution, process adherence and follow-up. |
| **What can it actually do?** | `directory.lookup` (read) · `directory.verify` (read) · `kb.search` (read) · `ticket.history` (read) · `ticket.create` (**write**, caller-confirmed) · `privileged.request` (**write**, queues for admin approval; never executes) |
| **What does it need to remember?** | Within the call: who it verified, what it already tried, what it promised. Across calls: this employee's previous tickets and resolutions, so a repeat of last week's problem is recognised as a repeat and escalated sooner rather than re-walking the same failed script. |
| **What should it never do?** | Never reset a credential, unlock an account or change access without a human approval. Never accept or read back a password, PIN or MFA code over voice. Never disclose another employee's data. Never invent a fix when `kb.search` returns nothing — it says it has no documented fix and escalates. Never claim a resolution the caller did not confirm. |
| **Human-in-the-loop** | Three points. (a) The caller confirms out loud before any ticket is filed. (b) An IT admin approves every privileged write from a queue. The call does not block on it — the caller is told a person will action it — but the action itself cannot occur until someone decides. (c) The post-call review flags calls for human audit — every escalation, every failed verification, and any call the reviewer scores below threshold. |
| **What happens when something breaks?** | Classified, not blanket-retried. *Transient* (timeout, 5xx): retry twice with backoff, then degrade. *Empty* (`kb.search` found nothing): never improvise — escalate and say why. *Down* (ITSM unreachable): write the ticket to a local outbox, tell the caller their reference will arrive by email, reconcile on recovery — the call still ends cleanly. *Unintelligible* (low ASR confidence twice running): offer a callback rather than guess. The invariant: **no failure path ends with the caller stranded, and none ends with a fabricated answer.** |
| **How do you know it worked?** | Primary: **containment with correctness** — share of calls resolved without a human, that an independent reviewer also judges correctly resolved and correctly processed. Target ≥ 80%. Reported alongside its counterweight, **false-containment** (agent claimed resolved, reviewer disagrees), which must be ~0 — an agent can trivially raise containment by declaring victory, so the two are never quoted apart. Secondary: median call length, escalation precision. |

---

## Why the reviewer runs on a different provider

The post-call reviewer decides whether the in-call agents did their job. Running it on
the same model that produced the transcript makes it a self-assessment, and self-
assessment is exactly the thing the metric is supposed to be robust to.

So the in-call agents run on OpenAI, and the **reviewer runs on Nebius Token Factory**
against an open-weights model. Different provider, different model family, different
failure modes. This also satisfies the cohort's requirement that at least one model
call route through Nebius — but the reason it is *that* call is architectural, not
box-ticking.

---

## Architecture

```
                    ElevenLabs Conversational AI  (voice loop, STT + TTS)
                                  │  custom tools over HTTPS webhooks
                                  ▼
                         FastAPI  (switchboard.server)
                                  │
                                  ▼
      ┌───────────────  LangGraph supervisor  ───────────────┐
      │                                                      │
   intake ──> verify ──┬── triage ──┬── resolve ──┬── close   │
                       │            │             │           │
                  (2 failures)      │        (no fix found)   │
                       └────────────┴──> escalate ────────────┘
                                            │
                                     privileged write?
                                            │
                              request only ──> approval queue ──> human admin
      └──────────────────────────────────────────────────────┘
                                  │
                            call ends
                                  ▼
                    review agent  (Nebius, independent)
                                  ▼
                 structured verdict + audit flag + caller memory
```

State is checkpointed to SQLite at every node, so a dropped call resumes with
everything the agent already established rather than starting the interrogation again.
