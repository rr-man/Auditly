# L1 desk reference — desk phones (demo)

## Phone shows "No Service" or "No Line"

A desk phone that shows No Service has power but is not registered with the phone system. Work
from the phone outwards:

1. Confirm the scope. Ask whether other phones at the site work. One phone down points to that
   phone, its cable or its wall jack; every phone down points to the internet connection, the
   firewall or a carrier outage.
2. Check power. The screen must be lit. Phones are powered over Ethernet (PoE) from the switch or
   from a power adapter; a dark screen is a power or PoE budget problem, not a registration problem.
3. Reseat the network cable at both ends — the back of the phone and the wall jack or switch port.
   A cable that is not clicked in is the most common cause. Wait 30 seconds for the phone to
   register; the extension number and the date return when it has.
4. If still No Service, reboot the phone (unplug the cable for ten seconds) and, if the phone sits
   behind a computer or a small switch, try the wall jack directly.
5. Still down: check the phone's IP address in its menu. No IP means DHCP or the switch port;
   an IP but no registration means the account, the SIP credentials or the firewall (SIP ALG
   should be OFF on the customer's router).
6. Escalate to Level 2 with the extension number, the phone's MAC address, what was tried and
   whether other phones work.

Always verify the caller (company and extension or account number) before changing anything on
the account, and close by confirming the phone shows the extension and can make a test call.

## Voicemail

Voicemail PINs are reset from the portal under Users › Voicemail; the default PIN is the last four
digits of the extension until changed. Messages older than 90 days are purged.

## Billing questions

Level 1 does not adjust invoices. Take the invoice number and the disputed line, open a billing
ticket and tell the caller finance replies within two business days.
