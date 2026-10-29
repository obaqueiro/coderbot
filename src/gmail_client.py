"""Send/receive correlated email threads via the Gmail API."""
import base64
import json
import logging
import mimetypes
import re
from html import escape
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

from googleapiclient.discovery import build

import config
import persistence
from command_text import COMMANDS, parse_command
from google_auth import load_credentials

try:
    import markdown as _markdown
except ImportError:  # keep sending (plain-text only) if the optional dep is missing
    _markdown = None

log = logging.getLogger(__name__)


def _markdown_to_html(text: str) -> str | None:
    """Render a Markdown body as a self-contained, Gmail-friendly HTML fragment.

    Gmail strips <head>/<style>, so we only emit basic inline-styled tags that it
    renders reliably. Returns None when the markdown lib is unavailable.
    """
    if _markdown is None:
        return None
    body = _markdown.markdown(
        text, extensions=["fenced_code", "tables", "sane_lists", "nl2br"])
    # Minimal inline styling for the few blocks Gmail won't style on its own.
    body = body.replace(
        "<pre>",
        '<pre style="background:#f6f8fa;padding:12px;border-radius:6px;'
        'overflow:auto;font-family:Menlo,Consolas,monospace;font-size:13px">')
    body = body.replace(
        "<code>",
        '<code style="background:#f6f8fa;padding:1px 4px;border-radius:4px;'
        'font-family:Menlo,Consolas,monospace;font-size:13px">')
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
            f'line-height:1.5;color:#1a1a1a">{body}</div>')


def _gmail():
    return build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)


def _strip_quoted(body: str) -> str:
    """Best-effort removal of quoted history from a reply body."""
    lines = []
    for line in body.splitlines():
        if re.match(r"^\s*On .{5,120} wrote:\s*$", line) or re.match(r"^\s*El .{5,120} escribi", line):
            break
        if line.startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _extract_body(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        text = _extract_body(part)
        if text:
            return text
    return ""


def send(subject: str, body: str, thread_id: str | None = None,
         attachments: list[Path] | None = None, *, progress: Path | None = None,
         delivery_id: str | None = None) -> str:
    """Send an email to the user; returns the Gmail thread id."""
    service = _gmail()
    msg = EmailMessage()
    # First address of the (possibly comma-separated) list; the rest are only
    # accepted as senders, not mailed.
    primary = config.USER_EMAIL.split(",")[0].strip()
    msg["To"] = primary
    msg["From"] = primary
    # Same account sends and receives; this header lets poll_reply skip our own mail.
    msg["X-Codebot"] = "1"
    if delivery_id:
        msg["Message-ID"] = f"<{delivery_id}@coderbot.local>"
    skipped = []

    if thread_id:
        # Reply within the existing thread: reuse its subject and last Message-Id.
        thread = service.users().threads().get(userId="me", id=thread_id, format="metadata").execute()
        headers = {h["name"].lower(): h["value"] for h in thread["messages"][-1]["payload"]["headers"]}
        msg["Subject"] = headers.get("subject", subject)
        if headers.get("message-id"):
            msg["In-Reply-To"] = headers["message-id"]
            prior = headers.get("references", "").strip()
            msg["References"] = f"{prior} {headers['message-id']}".strip()
        # Stamp the Gmail thread id so the user can grep it in the logs (the logs key
        # everything on this id). Only replies have it: a brand-new thread's id is
        # assigned by Gmail on send, after the body is fixed, so its first message can't
        # carry it — every later message in the thread does.
    else:
        msg["Subject"] = subject

    total = 0
    files = []
    for path in attachments or []:
        size = path.stat().st_size
        if total + size > config.MAX_ATTACHMENT_BYTES:
            skipped.append(path)
            continue
        total += size
        files.append(path)
    if skipped:
        log.warning("omitting %d attachment(s) over %d-byte cap: %s",
                    len(skipped), config.MAX_ATTACHMENT_BYTES, [str(p) for p in skipped])
        body += "\n\nAttachments omitted (size limit): " + ", ".join(str(p) for p in skipped)
    msg.set_content(body)
    # Attach an HTML alternative so Gmail renders Markdown instead of showing raw
    # '#', '*', '`' etc. Must be added before any attachment (multipart ordering).
    html = _markdown_to_html(body)
    if progress is not None:
        html = (html or f"<div>{escape(body).replace(chr(10), '<br>')}</div>")
        html += ('<p><img src="cid:codebot-progress" alt="'
                 + escape(progress.stem.replace('_', ' ').title(), quote=True)
                 + ' progress diagram" style="max-width:100%;height:auto"></p>')
    if html:
        msg.add_alternative(html, subtype="html")
        if progress is not None:
            html_part = msg.get_body(preferencelist=("html",))
            assert html_part is not None
            html_part.add_related(progress.read_bytes(), maintype="image",
                                  subtype="png", cid="<codebot-progress>",
                                  filename=progress.name)
    for path in files:
        from attachments import delivery_name
        ctype = mimetypes.guess_type(delivery_name(path))[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        log.debug("attaching %s (%s, %d bytes)", path.name, ctype, path.stat().st_size)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=delivery_name(path))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    sent = service.users().messages().send(userId="me", body=payload).execute()
    log.info("sent message id=%s thread=%s (%d attachment(s), %d bytes total)",
             sent["id"], sent["threadId"], len(files), total)
    return sent["threadId"]


def attachment_limits():
    return {"file": config.MAX_ATTACHMENT_BYTES - 65536, "mime": True}


def send_envelope(envelope, receipts, confirm):
    """Separate file messages make failed attachments independently retryable."""
    thread = envelope.get("thread_id")
    if receipts.get("body", {}).get("status") == "uncertain":
        return thread
    if receipts.get("body", {}).get("status") != "confirmed":
        confirm("body", {"status": "uncertain"})
        try:
            body = envelope["body"]
            for artifact in envelope["attachments"]:
                body = body.replace(f": attached {artifact['filename']}",
                                    f": delivery queued ({artifact['filename']})")
            thread = send(envelope["subject"], body, thread, delivery_id=envelope["id"] + "-body")
            confirm("body", {"status": "confirmed", "thread_id": thread})
        except Exception as error:
            confirm("body", {"status": "uncertain" if isinstance(error, (ConnectionError, TimeoutError)) else "failed",
                             "error": type(error).__name__})
            return thread
    else:
        thread = receipts["body"].get("thread_id", thread)
    for artifact in envelope["attachments"]:
        key = artifact["id"]
        if receipts.get(key, {}).get("status") in ("confirmed", "uncertain"):
            continue
        confirm(key, {"status": "uncertain"})
        try:
            thread = send(envelope["subject"], "Diagnostic/supporting file: " + artifact["filename"],
                          thread, [Path(artifact["path"])], delivery_id=envelope["id"] + "-" + key)
            confirm(key, {"status": "confirmed", "thread_id": thread})
        except Exception as error:
            confirm(key, {"status": "uncertain" if isinstance(error, (ConnectionError, TimeoutError)) else "failed",
                          "error": type(error).__name__})
    return thread


def reconcile_delivery(envelope, key, receipt):
    identity = envelope["id"] + "-" + key
    result = _gmail().users().messages().list(userId="me",
        q=f"in:sent rfc822msgid:{identity}@coderbot.local", maxResults=1).execute()
    messages = result.get("messages", [])
    if messages:
        return {"status": "confirmed", "thread_id": messages[0].get("threadId"),
                "provider_ids": [messages[0]["id"]]}
    return None


def _processed_path():
    return config.DATA_DIR / "processed_msgs.json"


def _load_processed() -> list[str]:
    processed = persistence.read_json(_processed_path(), [])
    return processed if isinstance(processed, list) else persistence.quarantine(_processed_path(), [])


def _save_processed(ids: list[str]) -> None:
    # Replies come from the same account, so UNREAD is never set on them;
    # we track consumed message ids ourselves instead. Insertion order, so
    # truncation drops the oldest entries.
    persistence.write_json(_processed_path(), ids[-500:])


def mark_processed(message_id: str) -> None:
    """Record a reply as consumed. Call this ONLY after the reply has been fully
    handled — marking it earlier drops the message for good if handling then crashes."""
    processed = _load_processed()
    if message_id not in processed:
        processed.append(message_id)
        _save_processed(processed)


# The leading "[<instance>]" tag every codebot subject starts with (after Re:/Fwd:).
_THREAD_TAG_RE = re.compile(r"^\s*(?:(?:re|fwd?):\s*)*\[([a-z0-9-]+)\]", re.IGNORECASE)


def command_for_me(body: str, subject: str) -> str | None:
    """The mailbox command a message addresses to THIS instance, or None.

    - `ABORT codebot-x7k2` targets that instance by name, wherever it is sent.
    - A bare command on a subject leading with an instance tag ("[codebot-x7k2] …",
      which every codebot thread's subject does) belongs to THAT instance ALONE —
      replying a bare ABORT on one bot's thread must not reset the whole fleet.
    - A bare ABORT or STATUS anywhere else (fresh mail, untagged subject) is the
      mailbox-wide last resort: every instance sharing the mailbox acts (fleet-wide
      stop / fleet status). A bare DONE / HOLD / CONTINUE elsewhere is ignored: they
      act on one instance's task, and guessing which instance was meant could strike
      or pause the wrong item in the backlog doc."""
    parsed = parse_command(body)
    if not parsed:
        return None
    command, target, _note = parsed
    if target:
        return command if target == config.INSTANCE_ID else None
    tag = _THREAD_TAG_RE.match(subject or "")
    if tag:
        return command if f"[{tag.group(1).lower()}]" == config.SUBJECT_PREFIX else None
    return command if command in ("ABORT", "STATUS") else None


def foreign_command(body: str) -> str | None:
    """The other-instance target of a command-shaped body ("ABORT codebot-x7k2"), or
    None when the body is not a command or addresses this instance. Wait states use
    this to keep a command aimed at ANOTHER instance — replied on one of OUR threads —
    from being classified as the user's answer (an "ABORT <other>" read as a
    conversational reply could abort the wrong task)."""
    parsed = parse_command(body)
    if not parsed:
        return None
    target = parsed[1]
    return target if target and target != config.INSTANCE_ID else None


def _from_matches(from_header: str, user_email: str) -> bool:
    """True if the From header's address equals one of the comma-separated addresses
    in user_email (case-insensitive)."""
    allowed = [e.strip().lower() for e in user_email.split(",") if e.strip()]
    return bool(allowed) and parseaddr(from_header)[1].lower() in allowed


def _is_draft(message: dict) -> bool:
    """True if the Gmail message is an unsent draft. threads().get and messages().list
    both return drafts alongside sent mail, and a draft reply carries the user's own
    From header, so without this check the bot reads (and acts on) a reply the user is
    still composing. A draft gets a fresh message id when it is actually sent, so
    skipping it here never hides the eventual real reply."""
    return "DRAFT" in message.get("labelIds", [])


def poll_command() -> tuple[str, str, str, bool, str] | None:
    """Scan recent mail for a user command; returns (message_id, thread_id, COMMAND,
    targeted, note) or None — `targeted` is True when the body named this instance
    explicitly ("DONE codebot-x7k2"), which callers treat as unambiguous; `note` is
    the free text after a CONTINUE.

    Mailbox-wide (any thread, or a brand-new email) rather than thread-scoped, so it works
    as a last resort even when the agent is stuck on a thread it no longer polls. Only honors
    a message whose From address is CODEBOT_USER_EMAIL (so a stranger cannot ABORT the bot),
    skips codebot's own mail (X-Codebot) and already-processed messages, and matches only when
    the reply body is exactly a known command (case-insensitive), optionally followed by an
    instance name, and addressed to this instance (see command_for_me) so it can't fire by
    accident — and so a command for one instance is never executed by another.
    """
    return _poll_command()


def poll_status(thread_id: str | None = None) -> tuple[str, str] | None:
    """Only look for status inquiries while the coding turn holds the main loop."""
    found = _poll_command(only_status=True)
    return (found[0], found[1]) if found else None


def poll_kick(thread_id: str | None = None) -> tuple[str, str] | None:
    """A targeted or task-thread KICK; never consume other mailbox commands."""
    found = _poll_command(only_kick=True)
    return (found[0], found[1]) if found else None


def poll_verify(thread_id: str | None = None):
    found = _poll_command(only_verify=True)
    if found and (found[3] or found[1] == thread_id):
        return found
    return None


def _poll_command(*, only_status: bool = False,
                  only_kick: bool = False, only_verify: bool = False) -> tuple[str, str, str, bool, str] | None:
    service = _gmail()
    processed = _load_processed()
    listing = service.users().messages().list(
        userId="me", q=("STATUS " if only_status else "KICK " if only_kick else "VERIFY " if only_verify else
                        "(ABORT OR STATUS OR KICK OR DONE OR HOLD OR PAUSE OR CONTINUE OR RESUME OR VERIFY) ")
                       + "newer_than:2d -in:drafts", maxResults=25).execute()
    for meta in listing.get("messages", []):
        if meta["id"] in processed:
            continue
        message = service.users().messages().get(userId="me", id=meta["id"], format="full").execute()
        if _is_draft(message):
            continue
        headers = {h["name"].lower(): h.get("value", "")
                   for h in message.get("payload", {}).get("headers", [])}
        if "x-codebot" in headers:
            continue
        if not _from_matches(headers.get("from", ""), config.USER_EMAIL):
            continue
        body = _strip_quoted(_extract_body(message.get("payload", {})))
        command = command_for_me(body, headers.get("subject", ""))
        if only_status and command != "STATUS":
            continue
        if only_kick and command != "KICK":
            continue
        if only_verify and command != "VERIFY":
            continue
        if command:
            _cmd, target, note = parse_command(body)
            log.warning("%s command received (msg %s, thread %s, targeted=%s)",
                        command, meta["id"], message.get("threadId"), bool(target))
            return meta["id"], message.get("threadId"), command, bool(target), note
    return None


def drain_thread(thread_id: str) -> int:
    """Mark every unprocessed user reply on the thread as processed WITHOUT handling
    it. For when the context the replies were written against no longer exists (the
    PR was rewritten by a conflict resolution): acting on a stale instruction —
    worst case an irreversible 'merge' — is worse than asking the user to re-send
    it. Returns how many replies were set aside; the caller tells the user."""
    drained = 0
    while (polled := poll_reply(thread_id)) is not None:
        msg_id, body = polled
        mark_processed(msg_id)
        drained += 1
        log.warning("set aside stale reply %s (%d chars) on thread %s",
                    msg_id, len(body), thread_id)
    return drained


def poll_reply(thread_id: str) -> tuple[str, str] | None:
    """Return (message_id, body) of the OLDEST unprocessed non-codebot reply, else None.

    Oldest-first (natural thread order) so that when the user sends several replies before
    a poll, they are handled in the order sent — one per tick — rather than newest-first,
    which executed a fresh instruction before a stale one and then replayed the stale one.

    Does NOT mark the message processed; the caller must call mark_processed() once it
    has successfully handled the reply. Marking here previously dropped the user's reply
    permanently whenever handling crashed afterwards (e.g. a transient claude failure).
    """
    service = _gmail()
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    processed = _load_processed()
    for msg_id, body in reply_candidates(thread.get("messages", []), processed, config.USER_EMAIL):
        if body:
            log.info("new reply in thread %s (msg %s, %d chars)", thread_id, msg_id, len(body))
            return msg_id, body
        # Empty after quote-strip: nothing to hand off, so consume it now to avoid rescan.
        mark_processed(msg_id)
        log.warning("reply msg %s had empty body after quote-strip; skipping", msg_id)
    return None


def reply_candidates(messages: list[dict], processed: list[str], user_email: str):
    """Yield (message_id, body) for each unprocessed non-codebot message FROM THE USER, in
    thread order (oldest first). body is '' when empty after quote-strip. Pure — no
    network/side effects, so poll_reply's ordering is unit-testable against a fake payload.

    The From check matters: Gmail threads by subject/references, so a third party mailing
    the user with a matching subject would otherwise be consumed as the user's reply and
    fed verbatim into the working Claude session (poll_command already filters this way).
    Foreign messages are skipped but NOT marked processed, so they stay visible in logs.

    Drafts are skipped too (and never marked processed): Gmail includes the user's
    half-written reply in the thread, and it must stay invisible until it is sent."""
    for message in messages:
        if message["id"] in processed:
            continue
        if _is_draft(message):
            log.debug("ignoring unsent draft %s in thread", message["id"])
            continue
        headers = {h["name"].lower(): h.get("value", "")
                   for h in message.get("payload", {}).get("headers", [])}
        if "x-codebot" in headers:
            continue
        if not _from_matches(headers.get("from", ""), user_email):
            log.warning("ignoring thread message %s from %r (not %s)",
                        message["id"], headers.get("from", ""), user_email)
            continue
        yield message["id"], _strip_quoted(_extract_body(message.get("payload", {})))
