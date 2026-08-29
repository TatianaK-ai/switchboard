"""Configuration: providers, models, paths, and the thresholds that gate behaviour.

Read once at import time. Anything that needs a different value (tests, evals) must
set the environment before the first `import switchboard.*` — see tests/conftest.py.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def _pick(*names: str) -> str:
    """First non-empty value among these env var names."""
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return ""


# --- providers -------------------------------------------------------------
# The in-call agents and the post-call reviewer deliberately run on different
# providers. The reviewer judges whether the in-call agents did their job; running
# both on one model turns that into self-assessment. See docs/FRAMEWORK.md.

OPENAI_API_KEY = _pick("OPENAI_API_KEY", "OPEN_API_KEY")
OPENAI_BASE_URL = _pick("OPENAI_BASE_URL") or None
CALL_MODEL = os.getenv("CALL_MODEL", "gpt-4.1-mini")

NEBIUS_API_KEY = _pick("NEBIUS_API_KEY")
NEBIUS_BASE_URL = os.getenv("NEBIUS_BASE_URL", "https://api.studio.nebius.ai/v1")
REVIEW_MODEL = os.getenv("REVIEW_MODEL", "meta-llama/Llama-3.3-70B-Instruct")

# When Nebius is not configured the reviewer falls back to OpenAI so the system still
# runs end to end — but it stops being independent, so it says so in its output rather
# than quietly reporting a number that means less than it appears to.
REVIEW_ON_NEBIUS = bool(NEBIUS_API_KEY)

ELEVENLABS_API_KEY = _pick("ELEVENLABS_API_KEY")
# Shared secret ElevenLabs sends on every tool webhook. Without it the server refuses
# writes: an open ticket-creation endpoint is a spam vector before it is anything else.
WEBHOOK_SECRET = _pick("WEBHOOK_SECRET")


@dataclass(frozen=True)
class Paths:
    data: Path
    runbooks: Path
    employees: Path
    db: Path
    checkpoints: Path


P = Paths(
    data=ROOT / os.getenv("DATA_DIR", "data"),
    runbooks=ROOT / os.getenv("DATA_DIR", "data") / "runbooks",
    employees=ROOT / os.getenv("DATA_DIR", "data") / "employees.json",
    db=ROOT / os.getenv("DATA_DIR", "data") / "switchboard.db",
    checkpoints=ROOT / os.getenv("DATA_DIR", "data") / "checkpoints.db",
)


# --- behaviour thresholds --------------------------------------------------
# These are policy, not tuning knobs. Each one was chosen for a stated reason and
# changing it changes what the agent is allowed to do.

# Two failed identity checks ends the self-serve path. One is a typo; two is either a
# confused caller or someone probing, and neither should reach a privileged tool.
MAX_VERIFY_ATTEMPTS = int(os.getenv("MAX_VERIFY_ATTEMPTS", "2"))

# Below this, kb.search is treated as having found nothing at all. The agent escalates
# rather than improvising from a weak match - a plausible wrong fix costs more than an
# honest handoff.
KB_MIN_SCORE = float(os.getenv("KB_MIN_SCORE", "48"))

# Transient tool failures retried this many times before degrading. Beyond two the
# caller is just listening to silence.
TOOL_RETRIES = int(os.getenv("TOOL_RETRIES", "2"))

# Two consecutive low-confidence transcriptions means the line or the accent is beating
# the ASR. Offer a callback instead of guessing at what was said.
MAX_UNCLEAR_TURNS = int(os.getenv("MAX_UNCLEAR_TURNS", "2"))
ASR_MIN_CONFIDENCE = float(os.getenv("ASR_MIN_CONFIDENCE", "0.55"))

# A call may not run forever; past this the agent wraps up and files a ticket.
MAX_TURNS = int(os.getenv("MAX_TURNS", "24"))


def assert_call_provider() -> None:
    if not OPENAI_API_KEY:
        raise SystemExit(
            "Missing OPENAI_API_KEY. Copy .env.example to .env and fill it in."
        )
