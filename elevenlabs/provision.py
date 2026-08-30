"""Create (or update) the ElevenLabs agent from the files in this directory.

    python elevenlabs/provision.py https://your-tunnel.trycloudflare.com

Doing this in code rather than by clicking through the dashboard means the agent
configuration is version-controlled and reproducible: the system prompt and the tool
schema live in this repo, and re-running this points a fresh agent at a new tunnel URL
without anyone remembering which boxes to tick.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv

HERE = pathlib.Path(__file__).resolve().parent
load_dotenv(HERE.parent / ".env")

API = "https://api.elevenlabs.io"
KEY = os.getenv("ELEVENLABS_API_KEY", "")
SECRET = os.getenv("WEBHOOK_SECRET", "")
AGENT_NAME = "Switchboard — IT Support"


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"xi-api-key": KEY, "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> {e.code}\n{e.read().decode()[:1500]}")


def build_config(base_url: str) -> dict:
    prompt = (HERE / "system-prompt.txt").read_text(encoding="utf-8").strip()

    # The single tool that carries the whole dialogue. Its description matters: it is
    # what stops the voice model deciding a greeting is too trivial to forward.
    turn_tool = {
        "type": "webhook",
        "name": "switchboard_turn",
        "description": (
            "Send the caller's utterance to the Switchboard decision service and get "
            "back the exact line to say. MUST be called for every caller turn, "
            "including greetings, one-word answers and confirmations. This service "
            "holds all conversation state, identity verification and policy - nothing "
            "may be decided without it."),
        "response_timeout_secs": 20,
        "api_schema": {
            "url": f"{base_url}/voice/turn",
            "method": "POST",
            "request_headers": {"X-Switchboard-Secret": SECRET},
            "request_body_schema": {
                "type": "object",
                "required": ["utterance", "call_id"],
                "properties": {
                    "utterance": {
                        "type": "string",
                        "description": ("Exactly what the caller just said. Do not "
                                        "clean it up, correct it or summarise it."),
                    },
                    # Filled by the platform, not by the model. Two earlier attempts
                    # failed here and both produced thread collisions: asking the LLM
                    # for it got the invented value "1", and putting the template in a
                    # request HEADER is not substituted at all - the server received
                    # the literal string "{{system__conversation_id}}" and every caller
                    # shared one conversation. A body property declared as a dynamic
                    # variable is the form that actually interpolates.
                    # Exactly one of description / dynamic_variable /
                    # is_system_provided / constant_value may be set - the API rejects
                    # the combination, which is how the right shape was found.
                    "call_id": {
                        "type": "string",
                        "dynamic_variable": "system__conversation_id",
                    },
                },
            },
        },
    }

    return {
        "name": AGENT_NAME,
        "conversation_config": {
            "agent": {
                "first_message": "IT support, this is Switchboard. What's going on?",
                "language": "en",
                "prompt": {
                    "prompt": prompt,
                    # Kept small deliberately: this model does not reason about the
                    # call, it relays. All judgement happens behind the webhook.
                    "llm": "gpt-4o-mini",
                    "temperature": 0.1,
                    "tools": [turn_tool],
                },
            },
            "turn": {"turn_timeout": 12},
        },
    }


def main() -> None:
    if not KEY:
        raise SystemExit("ELEVENLABS_API_KEY is not set in .env")
    if not SECRET:
        raise SystemExit("WEBHOOK_SECRET is not set in .env — refusing to expose an "
                         "unauthenticated webhook")
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)

    base = sys.argv[1].rstrip("/")
    cfg = build_config(base)

    existing = call("GET", "/v1/convai/agents").get("agents", [])
    mine = next((a for a in existing if a.get("name") == AGENT_NAME), None)

    if mine:
        aid = mine["agent_id"]
        call("PATCH", f"/v1/convai/agents/{aid}", cfg)
        print(f"updated existing agent {aid}")
    else:
        created = call("POST", "/v1/convai/agents/create", cfg)
        aid = created.get("agent_id")
        print(f"created agent {aid}")

    print(f"  webhook  -> {base}/voice/turn")
    print(f"  console  -> http://127.0.0.1:8080")
    print(f"  test it  -> https://elevenlabs.io/app/conversational-ai/agents/{aid}")


if __name__ == "__main__":
    main()
