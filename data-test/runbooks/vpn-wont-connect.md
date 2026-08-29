---
id: NET-021
title: VPN will not connect at all
path: connectivity
privileged: false
---
Applies when the VPN client fails to establish a session, on any network.

1. Confirm the error text on screen. "Authentication failed" is an account problem -
   switch to ACC-002. Anything else continues here.
2. Have them fully quit the client from the system tray, not just close the window.
3. Flush DNS: open Command Prompt and run `ipconfig /flushdns`.
4. Reconnect. If the client hangs at "Connecting", have them try the backup gateway
   listed in the client's dropdown.

If the backup gateway also fails, escalate to Network Ops. A whole-office outage looks
exactly like this from one desk, so mention their office location in the ticket.
