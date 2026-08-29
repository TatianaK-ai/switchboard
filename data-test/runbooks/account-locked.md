---
id: ACC-002
title: Account locked after failed sign-ins
path: account
privileged: true
---
Applies when sign-in reports the account is locked or disabled.

Unlocking an account is a privileged action. The agent may NOT perform it. Confirm
identity, then raise an approval request and tell the caller a member of the IT team
will release it.

1. Verify identity before anything else.
2. Check directory status. If status is 'suspended', this is not a lockout - it is an
   HR or offboarding hold, and IT must not unlock it. Route to HR Ops instead.
3. For a genuine lockout, raise an approval for `account.unlock`.
4. Tell the caller the request is with a human and roughly how long that takes.

Never read out, accept, or reset a password on the call.
