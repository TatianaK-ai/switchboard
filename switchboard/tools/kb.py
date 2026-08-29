"""Runbook search.

Nine documents. A vector store here would be architecture theatre - lexical scoring
over a corpus this size is both better and inspectable, and "why did it pick that
runbook" is a question a support engineer will actually ask.

The important behaviour is not the ranking. It is that a weak match returns EMPTY
rather than a plausible-looking wrong fix.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from rapidfuzz import fuzz

from ..config import KB_MIN_SCORE, P
from ..models import Risk
from .base import Failure, ToolError, maybe_fault, tool

_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


@lru_cache(maxsize=1)
def _runbooks() -> list[dict[str, Any]]:
    docs = []
    for path in sorted(P.runbooks.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        m = _FRONT.match(raw)
        meta, body = {}, raw
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = raw[m.end():]
        docs.append({
            "id": meta.get("id", path.stem),
            "title": meta.get("title", path.stem),
            "path": meta.get("path", "unknown"),
            "privileged": str(meta.get("privileged", "false")).lower() == "true",
            "body": body.strip(),
            "file": path.name,
        })
    return docs


_STOP = {
    "the", "and", "but", "with", "this", "that", "have", "has", "for", "from", "its",
    "was", "were", "are", "you", "your", "my", "me", "i", "it", "is", "am", "on", "in",
    "at", "of", "to", "a", "an", "so", "get", "got", "keeps", "keep", "cannot", "cant",
    "wont", "does", "doesnt", "not", "no", "some", "very", "really", "just", "when",
    "what", "why", "how", "need", "want", "please", "help", "issue", "problem", "work",
    "working", "broken", "trying", "tried",
}


def _stem(w: str) -> str:
    """Crudest possible stemmer, and deliberately so. Callers say 'it queues but never
    prints' where the runbook says 'jobs queue' and 'never print'; without this those
    are different tokens and a correct runbook scores as a miss."""
    for suf in ("ing", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _terms(text: str) -> set[str]:
    """Content words a runbook could plausibly be indexed on."""
    return {_stem(w) for w in re.findall(r"[a-z0-9]{3,}", text.lower())
            if w not in _STOP}


def _score(query: str, doc: dict[str, Any]) -> float:
    """Fuzzy title match, gated by whether the query's distinctive words appear at all.

    The gate is the part that matters. Fuzzy similarity alone rates "the coffee machine
    is broken" at 60 against a software-access runbook, because English filler words
    match everything - and a 60 that means nothing is worse than no score, since it
    sends the agent off to read out an irrelevant fix with confidence. Requiring the
    caller's actual content words to appear somewhere in the document collapses those
    to near zero while leaving genuine matches untouched.
    """
    q = query.lower()
    title = fuzz.token_set_ratio(q, doc["title"].lower())

    qt = _terms(query)
    if not qt:
        return 0.0
    dt = _terms(doc["title"] + " " + doc["body"])
    overlap = len(qt & dt) / len(qt)

    # No content word in common: this document is not about what was asked, whatever
    # the string similarity says.
    if overlap == 0.0:
        return 0.0
    return 0.55 * title + 45.0 * overlap


@tool("kb.search", Risk.NONE,
      "Search the IT runbooks for a documented fix. Pass the caller's symptom in their "
      "own words. Returns matching runbooks with their steps. If it returns nothing, "
      "there is no documented fix - say so and escalate. Never invent steps.")
def search(query: str, limit: int = 2) -> list[dict[str, Any]]:
    maybe_fault("kb.search")
    if not (query or "").strip():
        raise ToolError(Failure.BAD_INPUT, "empty query")

    ranked = sorted(((_score(query, d), d) for d in _runbooks()),
                    key=lambda p: p[0], reverse=True)
    hits = [{**d, "score": round(s, 1)} for s, d in ranked[:limit] if s >= KB_MIN_SCORE]

    if not hits:
        best = round(ranked[0][0], 1) if ranked else 0.0
        # EMPTY, not an exception the agent can shrug off: the caller gets an honest
        # "no documented fix" and an escalation, never an improvised one.
        raise ToolError(
            Failure.EMPTY,
            f"no runbook scored above {KB_MIN_SCORE} (best was {best})")
    return hits
