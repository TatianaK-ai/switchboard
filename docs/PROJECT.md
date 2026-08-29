# Switchboard — project documentation

Week 3, Mastering Agentic AI (August 2026 cohort). Use case 3F: Multi-Agent IT Support
Voice Agent. Code track: LangChain + LangGraph, with ElevenLabs Conversational AI as the
voice front end.

---

## 1. Overview

### What it does

An employee phones an IT support line. The agent takes the call from *"my laptop won't
connect"* to one of three honest endings:

- **Resolved** — it found a documented fix, walked them through it one step at a time,
  and the caller confirmed it worked.
- **Escalated** — it could not fix it, so it filed a ticket a human can act on without
  listening to the recording, having told the caller why.
- **Transferred** — it could not safely continue (identity unproven, line unusable) and
  said so.

Afterwards a separate reviewer, on a different provider, reads the transcript and judges
whether the call was actually resolved, whether the process was followed, and whether a
human needs to look at it.

### Why this shape

The brief says the hard parts are control flow, state, tool failure, and the autonomy
boundary. Each produced a design decision:

**Control flow is a graph, not a prompt.** A free-running ReAct loop decides its own next
step, which means the ordering "verify identity *before* touching anything privileged"
is a sentence in a prompt. Sentences in prompts are negotiable — the `injection` scenario
exists precisely to try. Here routing is Python in `graph/build.py`; the model is used at
the edges, to understand what was said and to choose words to say back. It never selects
a node.

**Guardrails are code paths.** `tools/privileged.py` contains no code that performs a
privileged action. Not a refusal branch — no branch at all. The strongest guarantee you
can give about an agent is that the capability is absent from the process, and that is
cheap to arrange when the tool layer is yours.

**Escalating is a correct outcome.** The reviewer is explicitly told a clean handoff can
score 5/5. This matters more than it sounds: containment is trivially maximised by
declaring every call resolved, so an agent rewarded for containment alone learns to
bluff. Every containment figure in this project is reported next to *false containment*.

### The agents

| Node | Job | Decides |
| --- | --- | --- |
| `ingest` | Records the utterance, checks ASR confidence | Whether we heard them at all |
| `intake` | Captures the problem and the employee id | Whether we have enough to proceed |
| `verify` | Checks a directory detail | Whether this caller is who they say |
| `triage` | Classifies into a support path | Self-serve or straight to a human |
| `resolve` | Searches runbooks, walks steps | Whether it is fixed — only on the caller's word |
| `escalate` | Files a ticket, requests approvals | What the human receives |
| `review` | Post-call, independent, on Nebius | Whether the call was actually handled well |

---

## 2. Datasets used

Everything is synthetic and committed, so the project is self-contained and no real
person's data is anywhere near it.

**Runbooks** — `data/runbooks/`, nine markdown documents with YAML front matter
(`id`, `title`, `path`, `privileged`). They are the *only* place a fix may come from; if
search returns nothing, the agent says it has no documented fix rather than assembling
one. Three are marked `privileged: true`, which the graph reads directly — a runbook can
declare that following it requires a human, and the agent then cannot complete it however
the conversation goes.

**Employee directory** — `data/employees.json`, five synthetic employees. Verification
uses the last four digits of a desk phone or an office city: things a colleague would
know and a stranger would not, and deliberately never a password, because the agent must
never handle one. One employee is `suspended`, which exists to test that IT does not
unlock what HR has held.

**Scenario scripts** — `switchboard/cli.py`, seven scripted callers driving the same
graph as the phone line. `evals/run.py` adds three degraded-dependency variants on top.

---

## 3. How it was built, and the prompts that did it

Built with Claude Code (Opus) over one working session, vibe-coded but not blind: the
framework in `docs/FRAMEWORK.md` was written *first* and acted as the spec, which is why
the guardrails are in the tool layer rather than bolted on afterwards.

Representative prompts, in the order they mattered:

> *"I would like to build Project 3F: Multi-Agent IT Support Voice Agent. Do it as a new
> project, use documentation and follow all required rules."*

The first useful move was not code. Before anything was written, the environment was
checked for credentials — which found no ElevenLabs and no Nebius key, and changed the
plan: the voice layer became a swappable adapter rather than a hard dependency, so
everything else could be built and tested without one.

> *"ElevenLabs front, LangGraph brain"*

This one decision shaped the architecture. The alternative — configuring six tools in
the ElevenLabs dashboard and letting its model orchestrate them — is faster to demo and
untestable. Choosing the single-tool pattern put the dialogue policy in Python where CI
can reach it.

Prompts that produced the most valuable work were the ones that named a *property* rather
than a feature:

- *"the guardrails should be code paths, not prompt text"* → the tool registry, and
  `privileged.py` having no execute branch at all.
- *"no failure path may end with the caller stranded"* → the failure taxonomy in
  `tools/base.py` and the three degraded scenarios in the eval suite.
- *"an agent that works on the happy path but falls over on the first tool failure is not
  finished"* (from the brief itself) → fault injection built into the tool layer, so
  outages are exercised rather than described.

What did **not** work: asking for "a multi-agent IT support system" in one go. The first
sketch had a supervisor agent choosing the next specialist with a model call, which is
the architecture everyone draws and the one that fails the injection test. Replacing the
supervisor with a `dict` lookup on `stage` made the system both simpler and safer.

---

## 4. Iterations

Numbered because the order matters; each was found by running the thing, not by reading it.

1. **Runbook search matched everything.** The first scorer combined fuzzy title match
   with `partial_token_set_ratio` over the body. Measured: *"the coffee machine is
   broken"* scored **60.3** against a software-access runbook, against **74.0** for a
   genuine VPN match. A threshold cannot separate those. The fix was to gate on
   *distinctive term overlap* — if none of the caller's content words appear anywhere in
   the document, the score is zero regardless of string similarity. Out-of-scope queries
   collapsed to 0–45, real hits stayed 50–100.

2. **…which then rejected two real matches.** *"printer queues but nothing prints"*
   scored 50.4 against a threshold of 55, because `queues`/`prints` are different tokens
   from `queue`/`print`. Added a four-line stemmer. Separation became: lowest true hit
   **50.0**, highest false positive **45.3**. The threshold sits at 48, and
   `test_kb_threshold_keeps_a_real_margin` fails if a future runbook edit narrows that
   gap — the margin is small enough to be worth guarding.

3. **The agent repeated itself.** On the happy path it gave step 3, the caller did it,
   and it gave step 3 again. The cause was embarrassing and instructive: agent replies
   were never written back into the transcript, so on each turn the model saw only the
   caller's half of the conversation and had no idea what it had already said. One line
   in `graph/build.py` — `update_state` appending the reply — fixed it. **A conversation
   the model cannot see is not a conversation.**

4. **The honest refusal was being swallowed.** When no runbook matched, `resolve` set a
   good line — *"I don't have a documented fix, so I'm not going to guess"* — and then
   `escalate` overwrote it with *"I'll log this as…"*. The caller heard the escalation
   but never the reason. Added `handoff_reason` to the state so the reason survives the
   node transition and is spoken. The behaviour was right; the caller just could not
   hear it, which is nearly as bad.

5. **Wording bugs that only appear out loud.** *"what are the the last four digits"*, and
   a handoff reason spliced mid-sentence without a capital or a full stop. Text output
   hides these; speech does not. Worth reading every agent line aloud once.

6. **The supervisor that was not needed.** Replacing a model-driven router with a
   dictionary removed a model call per turn, removed a latency source, removed a failure
   mode, and closed the injection vector. It is the change I would make first on any
   similar system.

---

## 5. Learnings

**A conversation the model cannot see is not a conversation.** Iteration 3 cost the most
time and had the smallest fix. Anything that is not in the transcript does not exist to
the next turn — and it is easy to build a system where the agent's own words are the
missing part.

**Fuzzy retrieval scores are not evidence of relevance.** A 60/100 that a broken coffee
machine earns against a software runbook is not a weak match, it is a meaningless number,
and thresholding it produces an agent that confidently reads out the wrong fix. Requiring
actual term overlap is cruder and far more honest. Measure the separation between what
should match and what should not; if there is no gap, there is no threshold.

**Put the guardrail where it cannot be argued with.** Every refusal in this system is a
code path with a test. The injection scenario passes not because the model resisted, but
because the function it would have had to call refuses unverified callers and the
function that performs the action does not exist. Prompt-level rules are for tone;
code-level rules are for safety.

**Retry policy needs a taxonomy.** Blanket retries are actively harmful: retrying an
EMPTY search burns seconds to get the same nothing while a caller waits, and retrying a
DENIED action is precisely how an agent talks itself past a guardrail. Five failure kinds
with five different responses turned out to be the smallest set that behaves sensibly.

**Escalation must be scored as success or the agent learns to bluff.** This is the same
finding as last week's evaluation work in a new costume: any metric that rewards
answering will produce answers, including for questions that should not have been
answered.

**One repetition is not evidence.** Carried forward deliberately, and the eval harness
refuses to let it be forgotten — a scenario that passes in some repetitions and not
others is reported as UNSTABLE rather than averaged into a percentage.

---

## 6. Honest limitations

- **Ten scripted callers are not a call centre.** Every script was written by the same
  person who wrote the agent, so they test the failures I anticipated. The failures I did
  not anticipate are, by construction, absent.
- **No real speech in the measured path.** Latency figures come from the text transport.
  ASR confidence is simulated by passing a number, not by a bad line in a noisy office.
- **The reviewer is a model.** Its verdicts are reported separately from the deterministic
  assertions, and never merged into one headline number, but a model judging a model
  remains a soft signal however it is dressed up.
- **The ITSM backend is a boolean.** The degraded path is genuinely exercised, but against
  a stub rather than a real ServiceNow instance with its own peculiar failure modes.
- **Caller-tier memory is read but barely used.** Ticket history is fetched at
  verification and passed to triage; a longer build would use it to shorten repeat calls
  rather than merely mention them.

## 7. How to reproduce

```bash
pip install -r requirements.txt
cp .env.example .env                  # OPENAI_API_KEY required; NEBIUS_API_KEY optional
pytest                                # 28 guardrail tests, no key needed
python -m evals.run --repeats 3       # the table in the README
python -m switchboard.server          # console at http://127.0.0.1:8080
```
