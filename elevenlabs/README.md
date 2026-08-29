# Wiring the voice front end

ElevenLabs Conversational AI runs the voice loop — speech in, speech out, turn taking,
interruption. It does **not** decide anything. Every turn is forwarded to the
LangGraph service, which returns the exact line to speak.

That split is deliberate. If the dialogue policy lived in the ElevenLabs system prompt
it could not be tested, could not be checkpointed, and could be talked around by a
caller who says the right thing. Here the prompt's only job is to be a mouth.

## 1. Expose the backend

```bash
python -m switchboard.server            # binds 127.0.0.1:8080
```

ElevenLabs needs to reach it, so put it behind a tunnel and set a secret first:

```bash
# .env
WEBHOOK_SECRET=<a long random string>

ngrok http 8080                          # or cloudflared tunnel --url http://localhost:8080
```

Without `WEBHOOK_SECRET` the server refuses to bind anything but loopback. An open
`/voice/turn` is a ticket-spam endpoint before it is anything else.

## 2. Create the agent

In the ElevenLabs dashboard: **Conversational AI → Agents → New agent**.

**System prompt** — paste `system-prompt.txt`. It is short on purpose.

**First message**

> IT support, this is Switchboard. What's going on?

**Tool** — add a Webhook tool named `switchboard_turn`, defined in `tool-turn.json`.
Point its URL at `https://<your-tunnel>/voice/turn` and add the header
`X-Switchboard-Secret: <your WEBHOOK_SECRET>`.

**Post-call webhook** — point at `https://<your-tunnel>/voice/ended`. This triggers the
independent review.

## 3. Check it

Use the dashboard's test call and say *"my VPN keeps dropping"*. You should hear the
agent ask for an employee id. Meanwhile:

```bash
curl localhost:8080/api/calls/<conversation_id> | python -m json.tool
```

…shows the graph state — stage, verification, steps taken — proving the decisions are
being made in Python and not in the prompt.

## Why one tool and not six

The obvious alternative is to give the ElevenLabs agent six tools (`lookup`, `verify`,
`search`, `create_ticket`…) and let its model orchestrate them. That is easier to set up
and worse in every way that matters here: the ordering of verification before privileged
work becomes a prompt instruction rather than a graph edge, state lives in a context
window rather than a checkpoint, and none of it can be run in CI.

One tool keeps the ElevenLabs layer replaceable. The same graph is driven by
`python -m switchboard.cli` with no voice at all, which is how every failure case in
`evals/` is exercised.
