---
id: ACC-011
title: Password expired or forgotten
path: account
privileged: true
---
Applies when the caller cannot sign in because the password expired or is forgotten.

1. Verify identity.
2. If the password is merely expired and they can still sign in, direct them to the
   self-service change page - no privileged action needed, and this is the outcome to
   aim for.
3. If they are fully locked out, raise an approval for `password.reset`.

The agent never sets, suggests, or receives a password. The reset link goes to the
employee's registered address after a human approves.
