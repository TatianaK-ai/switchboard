---
id: ACC-007
title: MFA on a replaced or lost phone
path: account
privileged: true
---
Applies when the caller has a new phone and cannot complete MFA.

Re-enrolment is privileged. Raise an approval; do not attempt it.

1. Verify identity. Because the usual second factor is exactly what is broken, use the
   directory detail (office, last four of the desk phone) rather than a push.
2. If the directory shows mfa_enrolled = false, they were never enrolled - this is an
   enrolment request, not a reset, and goes to the Identity queue.
3. Otherwise raise an approval for `mfa.reset`.
4. Warn them that after approval they must re-enrol from a trusted network.

Never accept a one-time code read aloud by the caller. If they start to, stop them.
