"""Schemas.

Every `Field(description=...)` here is serialised into the JSON schema sent to the
model and steers its decision as much as the system prompt does. They are not
documentation - treat them as prompt text.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Path_(str, Enum):
    """Support paths. The triage agent must choose exactly one."""
    ACCOUNT = "account"          # password, MFA, lockout
    CONNECTIVITY = "connectivity"  # VPN, wifi, network
    HARDWARE = "hardware"        # laptop, peripherals, docking
    SOFTWARE = "software"        # app installs, licences, access requests
    PRINTING = "printing"
    UNKNOWN = "unknown"          # nothing in the catalogue fits


class Risk(str, Enum):
    NONE = "none"
    CALLER_CONFIRM = "caller_confirm"   # reversible; the caller says yes out loud
    ADMIN_APPROVAL = "admin_approval"   # privileged; a human admin releases it


class Triage(BaseModel):
    """Where this call should go. Produced before any fix is attempted."""
    path: Path_ = Field(
        description="The support path this issue belongs to. Choose 'unknown' only "
                    "when the issue genuinely fits none of the others - an issue you "
                    "are unsure about but which is clearly, say, network-related is "
                    "'connectivity', not 'unknown'."
    )
    summary: str = Field(
        description="One sentence, in the caller's own terms, describing the problem. "
                    "This is what a human will read first if the call is escalated, so "
                    "it must be specific: 'VPN disconnects every few minutes on wifi "
                    "but not on ethernet', not 'network problem'."
    )
    needs_privileged_action: bool = Field(
        description="True if fixing this plausibly requires resetting a credential, "
                    "unlocking an account or changing group membership - anything only "
                    "an administrator may do. Being wrong in the True direction is "
                    "cheap (a human glances at it); being wrong in the False direction "
                    "means the agent walks the caller down a path it cannot finish."
    )
    urgency: Literal["low", "normal", "high"] = Field(
        description="'high' only when the caller is fully blocked from working right "
                    "now and no workaround exists. Not a measure of how annoyed they "
                    "sound."
    )


class ResolutionAttempt(BaseModel):
    """The outcome of walking a caller through one documented fix."""
    resolved: bool = Field(
        description="True only if the caller explicitly confirmed the problem is gone. "
                    "Silence, 'I think so', or the absence of a complaint is not "
                    "confirmation - if you did not hear a clear yes, this is False."
    )
    steps_taken: list[str] = Field(
        default_factory=list,
        description="Each step you actually walked the caller through, in order, in "
                    "past tense. A human reading the ticket must be able to see what "
                    "has already been ruled out so they do not repeat it."
    )
    blocked_reason: str = Field(
        default="",
        description="If not resolved, the specific reason: what step failed and what "
                    "the caller observed. Empty when resolved."
    )


class ReviewVerdict(BaseModel):
    """Post-call assessment. Produced by an independent reviewer, not the call agents."""
    resolved: bool = Field(
        description="Judging only from the transcript: did the caller end the call with "
                    "their problem actually fixed? An agent asserting it was fixed is "
                    "not evidence; look for the caller confirming."
    )
    process_followed: bool = Field(
        description="Was identity verified before anything sensitive, was escalation "
                    "used when the runbook ran out, and was every write either "
                    "caller-confirmed or admin-approved? False if any was skipped."
    )
    policy_violations: list[str] = Field(
        default_factory=list,
        description="Concrete breaches: a credential read aloud, another employee's "
                    "data disclosed, a fix invented with no runbook behind it, a "
                    "resolution claimed the caller never confirmed. Quote the moment."
    )
    followup_required: bool = Field(
        description="Does a human need to do something after this call, beyond any "
                    "ticket already filed?"
    )
    followup_reason: str = Field(
        default="", description="What the human needs to do. Empty if none."
    )
    quality: int = Field(
        ge=1, le=5,
        description="1 = actively harmful or misleading. 3 = did the job without grace. "
                    "5 = could not reasonably have gone better, including a clean "
                    "escalation when that was the right answer. An escalation is not "
                    "a failure and must not be scored as one."
    )
    reasoning: str = Field(
        description="Two sentences maximum, citing what in the transcript drove the "
                    "verdict."
    )
