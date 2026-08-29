"""Model access for the two providers, and the structured-output helper.

Two clients, deliberately:

- `call_llm`  — the in-call agents (OpenAI). Low latency matters; someone is holding
  a phone.
- `review_llm` — the post-call reviewer (Nebius Token Factory, open-weights). It judges
  whether the in-call agents did their job, so it must not be the same model marking
  its own homework.

If Nebius is not configured the reviewer falls back to OpenAI and every verdict it
writes is stamped `independent=False`, so a number produced by self-assessment can
never be mistaken for one that was not.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, TypeVar

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from .config import (CALL_MODEL, NEBIUS_API_KEY, NEBIUS_BASE_URL, OPENAI_API_KEY,
                     OPENAI_BASE_URL, REVIEW_MODEL, REVIEW_ON_NEBIUS)

T = TypeVar("T", bound=BaseModel)


@lru_cache(maxsize=1)
def call_llm() -> ChatOpenAI:
    kw: dict[str, Any] = {"api_key": OPENAI_API_KEY, "model": CALL_MODEL,
                          "temperature": 0, "timeout": 20, "max_retries": 2}
    if OPENAI_BASE_URL:
        kw["base_url"] = OPENAI_BASE_URL
    return ChatOpenAI(**kw)


@lru_cache(maxsize=1)
def review_llm() -> tuple[ChatOpenAI, str, bool]:
    """Returns (client, label, independent)."""
    if REVIEW_ON_NEBIUS:
        return (
            ChatOpenAI(api_key=NEBIUS_API_KEY, base_url=NEBIUS_BASE_URL,
                       model=REVIEW_MODEL, temperature=0, timeout=40, max_retries=2),
            f"nebius:{REVIEW_MODEL}",
            True,
        )
    return call_llm(), f"openai:{CALL_MODEL} (FALLBACK - not independent)", False


def extract(schema: type[T], system: str, conversation: str, *,
            reviewer: bool = False) -> T:
    """One structured-output call. Raises on failure; callers decide what that means -
    for the in-call graph it means 'take the escalation path', never 'guess'."""
    client = review_llm()[0] if reviewer else call_llm()
    structured = client.with_structured_output(schema)
    return structured.invoke(
        [{"role": "system", "content": system},
         {"role": "user", "content": conversation}]
    )


def speak(system: str, conversation: str) -> str:
    """Free-text reply for the caller. Kept separate from `extract` because the two
    have different failure modes: a malformed reply is recoverable, a malformed
    decision is not."""
    msg = call_llm().invoke(
        [{"role": "system", "content": system},
         {"role": "user", "content": conversation}]
    )
    return (msg.content or "").strip()
