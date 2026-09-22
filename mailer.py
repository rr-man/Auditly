"""Outbound email -- coaching invites, the agent's dispute link, the mail test -- the one place this
project speaks SMTP.

Stdlib smtplib over STARTTLS (or SMTPS) to a relay named in .env. Nothing is sent unless
SMTP_HOST is set; the password travels in the AUTH exchange only and never appears in a
message, a log line or an exception. The invite is a real calendar request: a
multipart/alternative body (plain text + text/calendar; method=REQUEST) with the .ics also
attached, so Outlook and Google Calendar show Accept / Decline.

Who the mail is from: SMTP_FROM (the mailbox that authenticates). The QA is the ORGANIZER in the
.ics and the Reply-To, so RSVPs and replies reach them. SMTP_SEND_AS_QA=1 puts the QA's address
in From instead -- only works when the relay lets that mailbox send as other users (Microsoft 365
"Send As"; most relays refuse and the send fails with a clear error)."""

import smtplib
import socket
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

TIMEOUT_S = 20


def status(c):
    """What the Server tab shows: configured?, host, from, mode. Never the password."""
    host = (c.get("SMTP_HOST") or "").strip()
    return {"configured": bool(host), "host": host or None, "port": _port(c),
            "from": (c.get("SMTP_FROM") or c.get("SMTP_USER") or "").strip() or None,
            "user_set": bool((c.get("SMTP_USER") or "").strip()), "password_set": bool((c.get("SMTP_PASSWORD") or "").strip()),
            "starttls": (c.get("SMTP_STARTTLS") or "1").strip() != "0",
            "send_as_qa": (c.get("SMTP_SEND_AS_QA") or "0").strip() == "1"}


def _port(c):
    try:
        return int((c.get("SMTP_PORT") or "587").strip())
    except ValueError:
        return 587


def problem(c):
    """Why an invite cannot be sent right now, or None."""
    st = status(c)
    if not st["configured"]:
        return "Email is not set up: add SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD and SMTP_FROM to .env (Settings › Server › Mail explains)."
    if not st["from"]:
        return "SMTP_FROM (or SMTP_USER) must name the sending mailbox."
    return None


def build_invite(session, to_email, qa_email, qa_name, ics_text, body_text, sender, reply_to=None, draft=False):
    """An EmailMessage carrying the calendar request. `sender` is the From address.
    draft=True makes an .eml the QA opens in their own mail app: X-Unsent: 1 tells Outlook to open it
    as a new, editable message (from the user's default account when From is blank)."""
    msg = EmailMessage()
    subject = session.get("title") or ("Coaching session: %s" % (session.get("agent_name") or "agent"))
    msg["Subject"] = subject
    if draft:
        msg["X-Unsent"] = "1"
    if sender:
        msg["From"] = ("%s <%s>" % (qa_name, sender)) if qa_name else sender
    if to_email:
        msg["To"] = to_email
    if reply_to and reply_to != sender:
        msg["Reply-To"] = reply_to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="auditly")
    msg.set_content(body_text)
    # the calendar part Outlook/Google act on
    msg.add_alternative(ics_text, subtype="calendar", params={"method": "REQUEST", "charset": "utf-8"})
    # and the same as an attachment for clients that only look for one
    msg.add_attachment(ics_text.encode("utf-8"), maintype="text", subtype="calendar", filename="coaching.ics",
                       params={"method": "REQUEST"})
    return msg


def build_plain(to_email, sender, subject, body_text, reply_to=None, sender_name=None):
    """A plain-text EmailMessage (the mail test, the agent's dispute link): the same headers as
    build_invite without the calendar parts. `sender` is the From address; `sender_name` its display name."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = ("%s <%s>" % (sender_name, sender)) if sender_name else sender
    msg["To"] = to_email
    if reply_to and reply_to != sender:
        msg["Reply-To"] = reply_to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="auditly")
    msg.set_content(body_text)
    return msg


def send(c, msg):
    """Deliver via the configured relay. Returns the Message-ID; raises RuntimeError with a
    redacted, user-facing reason on failure (never the password, never a stack trace)."""
    st = status(c)
    host, port = st["host"], st["port"]
    user, pw = (c.get("SMTP_USER") or "").strip(), (c.get("SMTP_PASSWORD") or "").strip()
    try:
        if port == 465 and not st["starttls"]:
            srv = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_S)
        else:
            srv = smtplib.SMTP(host, port, timeout=TIMEOUT_S)
        with srv:
            srv.ehlo()
            if st["starttls"] and port != 465:
                if srv.has_extn("starttls"):
                    srv.starttls()
                    srv.ehlo()
                else:
                    raise RuntimeError("The mail server at %s:%d does not offer STARTTLS; set SMTP_STARTTLS=0 only for a trusted internal relay." % (host, port))
            if user:
                srv.login(user, pw)
            srv.send_message(msg)
        return msg["Message-ID"]
    except smtplib.SMTPAuthenticationError:
        raise RuntimeError("The mail server refused the SMTP_USER / SMTP_PASSWORD sign-in. For Microsoft 365, SMTP AUTH must be enabled for that mailbox.")
    except smtplib.SMTPSenderRefused as e:
        raise RuntimeError("The mail server refused the From address (%s). With SMTP_SEND_AS_QA=1 the mailbox needs Send As rights for that user." % _clean(e))
    except smtplib.SMTPRecipientsRefused:
        raise RuntimeError("The mail server refused the recipient address.")
    except smtplib.SMTPException as e:
        raise RuntimeError("The mail server answered with an error: %s" % _clean(e))
    except (socket.timeout, TimeoutError):
        raise RuntimeError("Timed out talking to %s:%d." % (host, port))
    except OSError as e:
        raise RuntimeError("Could not reach %s:%d (%s)." % (host, port, e.__class__.__name__))


def _clean(e):
    s = str(e)
    for bad in ("password", "Password", "PASSWORD"):
        s = s.replace(bad, "***")
    return s[:200]
