---
id: PRT-003
title: Print jobs queue but never print
path: printing
privileged: false
---
1. Have them open the printer queue and cancel all jobs.
2. Restart the print spooler: Services > Print Spooler > Restart.
3. Re-send a single one-page test document.
4. If it still stalls, remove and re-add the printer by name from the office print
   server.

If the same printer is failing for multiple people it is a device or server fault, not
a desktop one. Ask whether colleagues are affected before going further - it changes
who the ticket goes to.
