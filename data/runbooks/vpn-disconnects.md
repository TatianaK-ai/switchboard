---
id: NET-014
title: VPN disconnects repeatedly on wifi
path: connectivity
privileged: false
---
Applies when the VPN client connects, then drops every few minutes, and the caller is
on wifi. If it also drops on a wired connection, this is not the right runbook - use
NET-021 (VPN will not connect at all).

1. Ask whether they are on a 2.4GHz or 5GHz network. Roaming between bands mid-session
   is the most common cause of this exact symptom.
2. Have them set the wifi adapter to prefer 5GHz: Settings > Network > Adapter options >
   Wireless > Configure > Advanced > Preferred Band > 5GHz.
3. Have them disable "Allow the computer to turn off this device to save power" on the
   same adapter's Power Management tab.
4. Reconnect the VPN and wait two minutes to confirm it holds.

If it still drops after step 4, escalate to Network Ops with the adapter model and the
office location - do not keep trying variations.
