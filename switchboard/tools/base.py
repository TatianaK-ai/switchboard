"""Tool plumbing: the read/write registry, the failure taxonomy, and retry policy.

The point of this module is that "is this a write?" and "should this be retried?" are
answered by code, not by a sentence in a prompt. A model can be argued out of a
sentence in a prompt.
"""
from __future__ import annotations

import functools
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from ..config import TOOL_RETRIES
from ..models import Risk


class Failure(str, Enum):
    """How a tool failed, which determines what the agent does next.

    Blanket-retrying everything is the mistake this exists to prevent: retrying an
    EMPTY result just spends time to get the same nothing, and retrying a DENIED
    action is how an agent talks itself past a guardrail.
    """
    TRANSIENT = "transient"   # timeout, 5xx, connection reset - worth retrying
    EMPTY = "empty"           # the call worked; there is genuinely no answer
    DOWN = "down"             # dependency unreachable - degrade, do not stall the call
    DENIED = "denied"         # refused by policy - never retry, never route around
    BAD_INPUT = "bad_input"   # the agent called it wrong - fix the call, do not repeat


class ToolError(Exception):
    def __init__(self, kind: Failure, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class ToolSpec:
    name: str
    risk: Risk
    description: str
    fn: Callable[..., Any]
    #: Retryable failures only; EMPTY/DENIED/BAD_INPUT never are, by construction.
    retries: int = TOOL_RETRIES


REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, risk: Risk, description: str, retries: int = TOOL_RETRIES):
    """Register a tool and wrap it in the retry policy its failure kind deserves."""
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return fn(*args, **kwargs)
                except ToolError as e:
                    if e.kind is not Failure.TRANSIENT or attempt >= retries:
                        raise
                    attempt += 1
                    # jittered backoff: a caller is listening, so keep it short
                    time.sleep(min(0.4 * 2 ** attempt, 2.0) * (0.5 + random.random()))
        REGISTRY[name] = ToolSpec(name, risk, description, wrapped, retries)
        return wrapped
    return deco


def writes() -> set[str]:
    return {n for n, s in REGISTRY.items() if s.risk is not Risk.NONE}


def requires_admin(name: str) -> bool:
    spec = REGISTRY.get(name)
    return bool(spec and spec.risk is Risk.ADMIN_APPROVAL)


@dataclass
class ToolResult:
    """Uniform envelope so the graph can branch on failure without try/except at
    every call site."""
    ok: bool
    value: Any = None
    failure: Failure | None = None
    message: str = ""
    tool: str = ""
    attempts: int = 1


def call(name: str, /, **kwargs) -> ToolResult:
    spec = REGISTRY.get(name)
    if spec is None:
        return ToolResult(False, failure=Failure.BAD_INPUT,
                          message=f"no such tool: {name}", tool=name)
    try:
        return ToolResult(True, value=spec.fn(**kwargs), tool=name)
    except ToolError as e:
        return ToolResult(False, failure=e.kind, message=e.message, tool=name)
    except TypeError as e:  # wrong arguments - the agent's mistake, not the tool's
        return ToolResult(False, failure=Failure.BAD_INPUT, message=str(e), tool=name)


#: Injected by tests and evals to simulate outages without touching the real code path.
FAULTS: dict[str, Failure] = field(default_factory=dict) if False else {}


def maybe_fault(name: str) -> None:
    """Raise the fault registered for this tool, if any. Called at the top of each
    tool so the eval suite can prove the degraded paths actually work."""
    kind = FAULTS.get(name)
    if kind:
        raise ToolError(kind, f"injected fault: {kind.value}")
