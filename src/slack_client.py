"""One Slack app per installation: Socket Mode inbox and per-task threads.

The socket callback only commits incoming messages; the existing FSM executes
them between agent turns. SQLite is also the persistent owned-thread registry.
"""
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path

import markdown

import config
import errors
from command_text import parse_command

log = logging.getLogger(__name__)
_socket = None
_web = None
_bot_user = None
_wake = threading.Event()
_last_reconcile: dict[str, float] = {}
_last_reconciled_at: dict[str, float] = {}
_RECONCILE_SECONDS = 60
_RECONCILE_OVERLAP_SECONDS = 300
_PROGRESS_INTERVAL_SECONDS = 30
_progress_lock = threading.RLock()
# A root is posted right after its nonce is saved, so a week of history covers any crash
# between the post and its registration; the page cap only stops a runaway scan.
_ROOT_SEARCH_SECONDS = 7 * 86400
_ROOT_SEARCH_PAGES = 200
# Replies that carry content; other subtypes (joins, edits, deletions) are not replies.
_REPLY_SUBTYPES = {None, "file_share", "thread_broadcast"}


@contextmanager
def _database():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.DATA_DIR / "slack_inbox.sqlite", timeout=15)
    db.execute("PRAGMA busy_timeout=15000")
    db.execute("CREATE TABLE IF NOT EXISTS roots (channel TEXT NOT NULL, root_ts TEXT NOT NULL, "
               "nonce TEXT UNIQUE NOT NULL, PRIMARY KEY(channel, root_ts))")
    db.execute("CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, channel TEXT NOT NULL, "
               "root_ts TEXT NOT NULL, ts TEXT NOT NULL, text TEXT NOT NULL, handled INTEGER NOT NULL DEFAULT 0)")
    db.execute("CREATE TABLE IF NOT EXISTS status_requests (id TEXT PRIMARY KEY, "
               "channel TEXT NOT NULL, ts TEXT NOT NULL, handled INTEGER NOT NULL DEFAULT 0)")
    db.execute("CREATE TABLE IF NOT EXISTS kick_requests (id TEXT PRIMARY KEY, "
                "channel TEXT NOT NULL, ts TEXT NOT NULL, handled INTEGER NOT NULL DEFAULT 0)")
    db.execute("CREATE TABLE IF NOT EXISTS progress (channel TEXT NOT NULL, root_ts TEXT NOT NULL, "
               "original TEXT NOT NULL, rendered TEXT, next_at REAL NOT NULL DEFAULT 0, "
               "terminal INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0, "
               "pending TEXT, pending_terminal INTEGER NOT NULL DEFAULT 0, "
               "PRIMARY KEY(channel, root_ts))")
    try:
        db.commit()
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _thread_id(channel: str, root_ts: str) -> str:
    return f"{channel}:{root_ts}"


def _split_thread(thread_id: str) -> tuple[str, str]:
    channel, root_ts = thread_id.split(":", 1)
    return channel, root_ts


def web():
    global _web
    if _web is None:
        from slack_sdk import WebClient
        from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
        _web = WebClient(token=config.SLACK_BOT_TOKEN, timeout=30)
        # Honour Slack's Retry-After before the error reaches the loop (errors.kind
        # then types what is left: ratelimited -> transient, invalid_auth -> fatal).
        _web.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=1))
    return _web


def validate() -> None:
    """Reject missing channel access before the first task is claimed."""
    global _bot_user
    info = web().auth_test()
    _bot_user = info["user_id"]
    headers = getattr(info, "headers", {}) or {}
    granted = {scope.strip() for scope in headers.get("x-oauth-scopes", "").split(",") if scope.strip()}
    required = {"chat:write", "channels:read", "channels:history", "files:write"}
    if granted and (missing := required - granted):
        raise RuntimeError(f"Slack bot token is missing scopes: {', '.join(sorted(missing))}")
    channel = web().conversations_info(channel=config.SLACK_CHANNEL_ID)["channel"]
    if channel.get("is_private") or not channel.get("is_channel"):
        raise RuntimeError("CODEBOT_SLACK_CHANNEL_ID must identify a public Slack channel")
    if not channel.get("is_member"):
        raise RuntimeError("Invite this instance's Slack app to the configured public channel")


def _accept_event(payload: dict) -> bool:
    event = payload.get("event") or {}
    if (event.get("type") != "message" or event.get("subtype") not in _REPLY_SUBTYPES or event.get("bot_id")
            or not event.get("user") or event.get("user") == _bot_user
            or event.get("channel") != config.SLACK_CHANNEL_ID):
        return False
    root = event.get("thread_ts")
    channel = event["channel"]
    if not event.get("ts"):
        return False
    if not root:
        # Bare KICK is task-scoped; only an explicitly targeted KICK can act
        # outside the task thread. Other top-level mutations remain ignored.
        parsed = parse_command(event.get("text") or "")
        if not parsed or (parsed[1] and parsed[1] != config.INSTANCE_ID):
            return False
        if parsed[0] == "STATUS":
            table = "status_requests"
        elif parsed[0] == "KICK" and parsed[1] == config.INSTANCE_ID:
            table = "kick_requests"
        else:
            return False
        with _database() as db:
            inserted = db.execute(
                f"INSERT OR IGNORE INTO {table}(id,channel,ts) VALUES(?,?,?)",
                (_thread_id(channel, event["ts"]), channel, event["ts"])).rowcount
        if inserted:
            _wake.set()
        return True
    if root == event["ts"]:
        return False
    with _database() as db:
        if not db.execute("SELECT 1 FROM roots WHERE channel=? AND root_ts=?",
                          (channel, root)).fetchone():
            log.warning("ignoring Slack reply in unregistered thread %s", _thread_id(channel, root))
            return False
        inserted = db.execute(
            "INSERT OR IGNORE INTO messages(id,channel,root_ts,ts,text) VALUES(?,?,?,?,?)",
            (_thread_id(channel, event["ts"]), channel, root, event["ts"],
             _event_text(event))).rowcount
    if inserted:
        log.info("Slack reply received in thread %s", _thread_id(channel, root))
        _wake.set()
    return True


def _event_text(event: dict) -> str:
    """The reply's text plus a line per attached file, so a reply that is only a
    screenshot or document is not dropped as empty."""
    files = [f"[attached file: {f.get('name') or f.get('title') or 'file'}"
             + (f" ({f['permalink']})" if f.get("permalink") else "") + "]"
             for f in event.get("files") or [] if isinstance(f, dict)]
    return "\n".join(part for part in [event.get("text") or "", *files] if part)


def _socket_request(client, request) -> None:
    from slack_sdk.socket_mode.response import SocketModeResponse
    try:
        if request.type == "events_api":
            _accept_event(request.payload)
    except Exception:
        # Do not acknowledge an event that could not be committed: Slack retries it.
        log.exception("failed to persist Slack event before acknowledgment")
        return
    client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))


def start() -> None:
    global _socket
    if _socket is not None:
        return
    validate()
    from slack_sdk.socket_mode import SocketModeClient
    client = SocketModeClient(app_token=config.SLACK_APP_TOKEN, web_client=web(),
                              auto_reconnect_enabled=True)
    client.socket_mode_request_listeners.append(_socket_request)
    client.connect()
    _socket = client


def wait(seconds: float) -> None:
    # Socket events wake immediately. A bounded timeout also reconciles events
    # missed during a disconnect or due to an incomplete Slack app subscription.
    _wake.wait(min(seconds, _RECONCILE_SECONDS))
    _wake.clear()


def announce(body: str) -> None:
    web().chat_postMessage(channel=config.SLACK_CHANNEL_ID, text=body,
                           unfurl_links=False)


def open_thread(state: dict) -> str:
    """Recover by nonce after an uncertain send; never create a second root."""
    nonce = state.get("thread_nonce") or state.get("branch")
    if not nonce:
        raise RuntimeError("cannot start a Slack task thread without a persisted task nonce")
    channel = config.SLACK_CHANNEL_ID
    with _database() as db:
        found = db.execute("SELECT root_ts FROM roots WHERE nonce=?", (nonce,)).fetchone()
    if found:
        return _thread_id(channel, found[0])
    marker = f"[codebot-task:{nonce}]"
    # A crash may have occurred after Slack accepted the post but before its ID
    # reached state.json or SQLite. Search our own roots before posting again.
    # Bounded by time, not pages: a busy channel must not leave the task unable to start.
    cursor, oldest = None, f"{time.time() - _ROOT_SEARCH_SECONDS:.6f}"
    for _page in range(_ROOT_SEARCH_PAGES):
        args = {"channel": channel, "limit": 200, "oldest": oldest}
        if cursor:
            args["cursor"] = cursor
        page = web().conversations_history(**args)
        match = next((msg for msg in page.get("messages", [])
                      if msg.get("user") == _bot_user and marker in msg.get("text", "")), None)
        if match:
            root_ts = match["ts"]
            break
        cursor = (page.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            root_ts = None
            break
    else:
        # A truncated history is ambiguous; never risk a duplicate root.
        raise errors.Transient(f"cannot reconcile Slack task root {marker} in channel history",
                               code="slack_history_truncated", resource="slack")
    if not root_ts:
        title = state.get("item", "(unnamed task)")
        link = state.get("item_url")
        text = state.get("thread_intro") or (f"*{config.INSTANCE_ID}* picked: {title}" +
                                             (f"\nSource: {link}" if link else ""))
        root_ts = web().chat_postMessage(channel=channel, text=f"{text}\n{marker}",
                                          unfurl_links=False)["ts"]
    with _database() as db:
        db.execute("INSERT OR IGNORE INTO roots(channel,root_ts,nonce) VALUES(?,?,?)",
                   (channel, root_ts, nonce))
        original = match["text"] if match else f"{text}\n{marker}"
        db.execute("INSERT OR IGNORE INTO progress(channel,root_ts,original) VALUES(?,?,?)",
                   (channel, root_ts, original))
    return _thread_id(channel, root_ts)


def close_thread(thread_id: str | None) -> None:
    if not thread_id:
        return
    channel, root_ts = _split_thread(thread_id)
    with _progress_lock, _database() as db:
        db.execute("DELETE FROM roots WHERE channel=? AND root_ts=?", (channel, root_ts))
        db.execute("DELETE FROM progress WHERE channel=? AND root_ts=? AND pending IS NULL", (channel, root_ts))


def _render_progress(original: str, snapshot: dict) -> str:
    spanish = snapshot.get("language", "English").casefold() == "spanish"
    phase = snapshot.get("phase", "")
    phase = {"EXPLORING": "Explore", "PROPOSING": "Proposal", "IMPLEMENTING": "Implement",
             "VERIFYING": "Verify", "INTERNAL_REVIEW": "Internal Review", "ARCHIVING": "Archive",
             "OPEN_PR": "Open PR"}.get(phase, str(phase).replace("_", " ").title())
    situation = snapshot.get("situation")
    emoji = ""
    if situation == "working":
        emoji = " :loading:"
    elif situation == "waiting_input":
        wait = snapshot.get("waiting_for")
        label = ({"WAIT_APPROVAL": "esperando aprobación", "WAIT_MERGE": "esperando merge"}.get(
            wait, "esperando tu respuesta") if spanish else
            {"WAIT_APPROVAL": "awaiting approval", "WAIT_MERGE": "awaiting merge"}.get(wait, "awaiting your response"))
        phase += f" — {label}"
        emoji = " :question:"
    elif situation in ("paused", "completed", "aborted"):
        phase = ({"paused": "En pausa", "completed": "Finalizado", "aborted": "Cancelado"} if spanish else
                 {"paused": "Paused", "completed": "Completed", "aborted": "Aborted"})[situation]
    lines = snapshot.get("log_lines", [])[-3:]
    # Metadata is plain text; escape Slack mentions and keep log fences intact.
    tail = "\n```\n" + "\n".join(_escape_slack(line.replace("```", "'''"))[:700]
                                  for line in lines) + "\n```" if lines else ""
    links = []
    for key, label in (("pr_url", "PR"), ("video_url", "Video")):
        url = snapshot.get(key)
        if isinstance(url, str) and re.fullmatch(r"https://[^\s<>|]+", url):
            links.append(f"{label}: <{_escape_slack(url)}|{label}>")
    resources = "\n\n" + "\n".join(links) if links else ""
    return original + resources + f"\n\n{'Estado' if spanish else 'Status'}: {_escape_slack(phase)}{emoji}" + tail


def update_progress(thread_id: str, snapshot: dict) -> None:
    """Edit only an owned root, deduplicating across restarts and respecting 429s."""
    channel, root = _split_thread(thread_id)
    with _progress_lock:
        with _database() as db:
            if not db.execute("SELECT 1 FROM roots WHERE channel=? AND root_ts=?", (channel, root)).fetchone():
                return
            row = db.execute("SELECT original,rendered,next_at,terminal,retry_at,pending_terminal FROM progress WHERE channel=? AND root_ts=?",
                             (channel, root)).fetchone()
        if row is None:
            page = web().conversations_replies(channel=channel, ts=root, limit=1)
            message = next((msg for msg in page.get("messages", [])
                            if msg.get("ts") == root and msg.get("user") == _bot_user), None)
            if not message:
                return
            row = (message["text"], None, 0, 0, 0, 0)
            with _database() as db:
                db.execute("INSERT OR IGNORE INTO progress(channel,root_ts,original) VALUES(?,?,?)",
                           (channel, root, row[0]))
        original, previous, next_at, terminal, retry_at, pending_terminal = row
        rendered = _render_progress(original, snapshot)
        final = snapshot.get("situation") in ("completed", "aborted")
        if terminal or pending_terminal:
            return
        if rendered == previous:
            with _database() as db:
                db.execute("UPDATE progress SET pending=NULL WHERE channel=? AND root_ts=?", (channel, root))
            return
        with _database() as db:
            db.execute("UPDATE progress SET pending=?,pending_terminal=? WHERE channel=? AND root_ts=?",
                       (rendered, int(final), channel, root))
        if time.time() < retry_at or (time.time() < next_at and not final
                                     and snapshot.get("situation") != "paused"):
            return
        _send_progress(channel, root, rendered, final)


def _send_progress(channel: str, root: str, rendered: str, final: bool) -> None:
    try:
        web().chat_update(channel=channel, ts=root, text=rendered, mrkdwn=True)
    except Exception as error:
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", {}) or {}
        try:
            retry = float(headers.get("Retry-After", headers.get("retry-after", 5)))
        except (ValueError, TypeError):
            retry = 5
        with _database() as db:
            db.execute("UPDATE progress SET retry_at=? WHERE channel=? AND root_ts=?",
                       (time.time() + max(5, retry), channel, root))
        raise
    with _database() as db:
        db.execute("UPDATE progress SET rendered=?,next_at=?,terminal=?,retry_at=0,"
                   "pending=NULL,pending_terminal=0 WHERE channel=? AND root_ts=?",
                    (rendered, time.time() + _PROGRESS_INTERVAL_SECONDS, int(final), channel, root))
        db.execute("DELETE FROM progress WHERE channel=? AND root_ts=? AND NOT EXISTS "
                   "(SELECT 1 FROM roots WHERE channel=? AND root_ts=?)", (channel, root, channel, root))


def flush_progress() -> None:
    with _progress_lock:
        with _database() as db:
            rows = db.execute("SELECT channel,root_ts,pending,pending_terminal FROM progress "
                              "WHERE pending IS NOT NULL AND retry_at<=? AND (next_at<=? OR pending_terminal=1)",
                              (time.time(), time.time())).fetchall()
        for channel, root, rendered, final in rows:
            try:
                _send_progress(channel, root, rendered, bool(final))
            except Exception:
                log.warning("could not retry Slack task progress")


def _escape_slack(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class _SlackMarkdown(HTMLParser):
    """Translate rendered Markdown into Slack's smaller mrkdwn vocabulary."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.lists: list[int | None] = []
        self.pre = False
        self.after_br = False
        self.link: tuple[int, str] | None = None

    def _break(self, count: int = 1) -> None:
        text = "".join(self.parts)
        if text:
            existing = len(text) - len(text.rstrip("\n"))
            self.parts = [text + "\n" * max(0, count - existing)]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "pre"):
            self._break(2)
        if tag in ("strong", "b", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("*")
        elif tag in ("em", "i"):
            self.parts.append("_")
        elif tag in ("del", "s", "strike"):
            self.parts.append("~")
        elif tag == "code" and not self.pre:
            self.parts.append("`")
        elif tag == "pre":
            self.pre = True
            self.parts.append("```\n")
        elif tag == "br":
            self.parts.append("\n")
            self.after_br = True
        elif tag == "hr":
            self._break(2)
            self.parts.append("———")
            self._break(2)
        elif tag in ("ul", "ol"):
            self._break()
            self.lists.append(1 if tag == "ol" else None)
        elif tag == "li":
            self._break()
            number = self.lists[-1]
            self.parts.append("  " * (len(self.lists) - 1) +
                              (f"{number}. " if number is not None else "• "))
            if number is not None:
                self.lists[-1] = number + 1
        elif tag == "a":
            self.link = (len(self.parts), dict(attrs).get("href") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("strong", "b", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("*")
        elif tag in ("em", "i"):
            self.parts.append("_")
        elif tag in ("del", "s", "strike"):
            self.parts.append("~")
        elif tag == "code" and not self.pre:
            self.parts.append("`")
        elif tag == "pre":
            self._break()
            self.parts.append("```")
            self.pre = False
        elif tag == "a" and self.link:
            start, url = self.link
            label = "".join(self.parts[start:])
            if url.startswith(("https://", "http://", "mailto:")):
                self.parts[start:] = [f"<{_escape_slack(url).replace('|', '%7C')}|{label}>"]
            self.link = None
        if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "pre"):
            self._break(2)
        elif tag == "li":
            self._break()
        elif tag in ("ul", "ol"):
            self.lists.pop()
            self._break(2)

    def handle_data(self, data: str) -> None:
        if self.after_br:
            data = data.removeprefix("\n")
            self.after_br = False
        if not self.pre and not data.strip() and "\n" in data:
            return  # HTML source whitespace after <br> / between block tags
        self.parts.append(_escape_slack(data))


def _to_mrkdwn(body: str) -> str:
    html = markdown.markdown(body, extensions=["fenced_code", "sane_lists", "nl2br"])
    parser = _SlackMarkdown()
    parser.feed(html)
    return "".join(parser.parts).strip("\n")


def _chunks(body: str, limit: int = 3800):
    fence_open = False
    prefix = ""
    while len(body) + len(prefix) > limit:
        # Leave room for closing/reopening a code fence across messages.
        room = limit - len(prefix) - 4
        index = body.rfind("\n", 0, room)
        if index < room // 2:
            index = room
        else:
            index += 1  # keep the boundary newline in the outgoing text
        part = body[:index]
        body = body[index:]
        fence_open ^= sum(line.strip() == "```" for line in part.splitlines()) % 2 == 1
        yield prefix + part + ("\n```" if fence_open else "")
        prefix = "```\n" if fence_open else ""
    if body:
        yield prefix + body


def send(subject: str, body: str, thread_id: str | None = None,
         attachments: list[Path] | None = None, *, progress: Path | None = None) -> str:
    if not thread_id:
        raise RuntimeError("Slack task messages require an owned task thread")
    channel, root_ts = _split_thread(thread_id)
    # Upload evidence first so the message's index describes only files Slack accepted.
    failed = []
    for path in attachments or []:
        try:
            from attachments import delivery_name
            body = body.replace(path.name, delivery_name(path))
            web().files_upload_v2(channel=channel, thread_ts=root_ts,
                                  file=str(path), filename=delivery_name(path))
        except Exception:
            log.exception("could not share %s in Slack thread", path.name)
            failed.append(path)
            body = body.replace(f": attached {path.name}", f": unavailable ({path.name})")
    if failed:
        body += "\n\nEvidence file unavailable: " + ", ".join(path.name for path in failed)
    for part in _chunks(_to_mrkdwn(body)):
        web().chat_postMessage(channel=channel, thread_ts=root_ts, text=part,
                                mrkdwn=True, unfurl_links=False)
    for path in [progress] if progress else []:
        try:
            web().files_upload_v2(channel=channel, thread_ts=root_ts,
                                  file=str(path), filename=path.name)
        except Exception:
            log.exception("could not share %s in Slack thread", path.name)
            web().chat_postMessage(channel=channel, thread_ts=root_ts,
                                   text=f"Evidence file unavailable: {path.name}")
    return thread_id


def send_envelope(envelope, receipts, confirm):
    """Upload each file once; the caller persists receipts before the next send."""
    channel, root = _split_thread(envelope["thread_id"])
    for artifact in envelope["attachments"]:
        key = artifact["id"]
        if receipts.get(key, {}).get("status") in ("confirmed", "uncertain"):
            continue
        confirm(key, {"status": "uncertain"})
        try:
            if artifact["size"] == 0:
                # Slack rejects zero-byte uploads. Deliver an explicit empty-output
                # descriptor, never invent a successful command/test result. This
                # also repairs notifications queued by older versions of the bot.
                result = web().files_upload_v2(channel=channel, thread_ts=root,
                    content=f"Original file: {artifact['filename']}\nOriginal size: 0 bytes.\n"
                            "The source file contains no output. This does not establish a check result.\n",
                    filename=artifact["filename"])
            else:
                result = web().files_upload_v2(channel=channel, thread_ts=root,
                    file=artifact["path"], filename=artifact["filename"])
            ids = [file.get("id") for file in result.get("files", [])]
            confirm(key, {"status": "confirmed", "provider_ids": ids})
        except Exception as error:
            confirm(key, {"status": "uncertain" if isinstance(error, (ConnectionError, TimeoutError)) else "failed",
                           "error": str(error)[:500]})
            log.warning("Slack attachment delivery failed for %s: %s", artifact["filename"], error)
    if receipts.get("body", {}).get("status") not in ("confirmed", "uncertain"):
        pending = [a["filename"] for a in envelope["attachments"]
                   if receipts.get(a["id"], {}).get("status") != "confirmed"]
        body = envelope["body"]
        if pending:
            for filename in pending:
                body = body.replace(f": attached {filename}", f": pending delivery ({filename})")
            body += "\nFiles pending delivery: " + ", ".join(pending)
        confirm("body", {"status": "uncertain"})
        ids = []
        try:
            for part in _chunks(_to_mrkdwn(body)):
                result = web().chat_postMessage(channel=channel, thread_ts=root, text=part,
                    client_msg_id=envelope["id"], mrkdwn=True, unfurl_links=False)
                ids.append(result.get("ts"))
            confirm("body", {"status": "confirmed", "provider_ids": ids})
        except Exception as error:
            confirm("body", {"status": "uncertain" if isinstance(error, (ConnectionError, TimeoutError)) else "failed",
                             "error": type(error).__name__})
    return envelope["thread_id"]


def attachment_limits():
    return {"file": getattr(config, "SLACK_MAX_ATTACHMENT_BYTES", 100 * 1024 * 1024),
            "mime": False}


def reconcile_delivery(envelope, key, receipt):
    channel, root = _split_thread(envelope["thread_id"])
    try:
        cursor = None
        while True:
            args = {"channel": channel, "ts": root, "limit": 100}
            if cursor:
                args["cursor"] = cursor
            page = web().conversations_replies(**args)
            artifact = next((a for a in envelope["attachments"] if a["id"] == key), None)
            for message in page.get("messages", []):
                if key == "body" and message.get("client_msg_id") == envelope["id"]:
                    return {"status": "confirmed", "provider_ids": [message["ts"]]}
                for file in message.get("files", []):
                    if artifact and file.get("name") == artifact["filename"] and file.get("size") == artifact["size"]:
                        return {"status": "confirmed", "provider_ids": [file["id"]]}
            cursor = page.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    except Exception:
        log.warning("could not reconcile uncertain Slack delivery")
    return None


def _pending(thread_id: str | None = None):
    clause, args = "", []
    if thread_id:
        channel, root_ts = _split_thread(thread_id)
        clause = " AND m.channel=? AND m.root_ts=?"
        args = [channel, root_ts]
    with _database() as db:
        return db.execute(
            "SELECT m.id,m.text,m.channel,m.root_ts FROM messages m "
            "JOIN roots r ON r.channel=m.channel AND r.root_ts=m.root_ts "
            "WHERE m.handled=0" + clause + " ORDER BY CAST(m.ts AS REAL),m.ts", args).fetchall()


def _reconcile(thread_id: str) -> None:
    """Backfill missed socket events at most once a minute per owned thread.

    Scan the full thread once per process. Subsequent scans overlap the preceding
    successful scan by five minutes, so late events are still recovered without
    repeatedly downloading the entire (potentially huge) conversation.
    """
    now = time.monotonic()
    if now - _last_reconcile.get(thread_id, float("-inf")) < _RECONCILE_SECONDS:
        return
    _last_reconcile[thread_id] = now
    channel, root_ts = _split_thread(thread_id)
    with _database() as db:
        if not db.execute("SELECT 1 FROM roots WHERE channel=? AND root_ts=?",
                          (channel, root_ts)).fetchone():
            log.warning("cannot reconcile unregistered Slack thread %s", thread_id)
            return
    previous = _last_reconciled_at.get(thread_id)
    oldest = (max(float(root_ts), previous - _RECONCILE_OVERLAP_SECONDS)
              if previous is not None else float(root_ts))
    oldest_arg = f"{oldest:.6f}" if previous is not None and oldest > float(root_ts) else root_ts
    scan_started = time.time()
    cursor = None
    try:
        while True:
            args = {"channel": channel, "ts": root_ts, "oldest": oldest_arg,
                    "limit": 100, "inclusive": False}
            if cursor:
                args["cursor"] = cursor
            page = web().conversations_replies(**args)
            for message in page.get("messages", []):
                if message.get("ts") != root_ts:
                    _accept_event({"event": {**message, "type": "message",
                                              "thread_ts": root_ts, "channel": channel}})
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        _last_reconciled_at[thread_id] = scan_started
    except Exception:
        log.exception("could not reconcile Slack replies in %s; retrying later", thread_id)


def poll_command():
    """Recover missed commands even when no task thread is currently active.

    Held threads remain registered in roots. Reconcile every owned thread using
    the same per-thread throttle as reply/status polling, then inspect the durable
    inbox. Retired threads are excluded and processed messages remain deduplicated.
    """
    with _database() as db:
        roots = db.execute("SELECT channel,root_ts FROM roots").fetchall()
    for channel, root_ts in roots:
        _reconcile(_thread_id(channel, root_ts))
    for msg_id, text, channel, root_ts in _pending():
        parsed = parse_command(text)
        if not parsed:
            continue
        command, target, note = parsed
        if target and target != config.INSTANCE_ID:
            mark_processed(msg_id)
            continue
        return msg_id, _thread_id(channel, root_ts), command, bool(target), note
    return None


def poll_status(thread_id: str | None = None) -> tuple[str, str] | None:
    """Fetch STATUS; recover missed Socket Mode events during long agent turns.

    poll_reply already reconciles while waiting for a user. During implementation
    the main loop never calls it, so without this reconciliation the independent
    status supervisor sees an empty SQLite inbox even when Slack has replies.
    _reconcile throttles the Web API call to at most once per minute per thread.
    """
    with _database() as db:
        rows = db.execute("SELECT id,channel,ts FROM status_requests WHERE handled=0 "
                          "ORDER BY CAST(ts AS REAL),ts").fetchall()
    if rows:
        msg_id, channel, ts = rows[0]
        return msg_id, _thread_id(channel, ts)
    if thread_id:
        _reconcile(thread_id)
        for msg_id, text, channel, root_ts in _pending(thread_id):
            parsed = parse_command(text)
            if parsed and parsed[0] == "STATUS" and (not parsed[1] or parsed[1] == config.INSTANCE_ID):
                return msg_id, _thread_id(channel, root_ts)
    return None


def poll_kick(thread_id: str | None = None) -> tuple[str, str] | None:
    """Only KICK, with the same owned-thread and reconciliation safeguards."""
    with _database() as db:
        row = db.execute("SELECT id,channel,ts FROM kick_requests WHERE handled=0 "
                         "ORDER BY CAST(ts AS REAL),ts LIMIT 1").fetchone()
    if row:
        msg_id, channel, ts = row
        return msg_id, _thread_id(channel, ts)
    if thread_id:
        _reconcile(thread_id)
        for msg_id, text, channel, root_ts in _pending(thread_id):
            parsed = parse_command(text)
            if parsed and parsed[0] == "KICK" and (not parsed[1] or parsed[1] == config.INSTANCE_ID):
                return msg_id, _thread_id(channel, root_ts)
    return None


def poll_verify(thread_id: str | None = None):
    """Find an explicit VERIFY for this active thread without consuming other commands."""
    if not thread_id:
        return None
    _reconcile(thread_id)
    for msg_id, text, channel, root_ts in _pending(thread_id):
        parsed = parse_command(text)
        if parsed and parsed[0] == "VERIFY" and (not parsed[1] or parsed[1] == config.INSTANCE_ID):
            return msg_id, _thread_id(channel, root_ts), "VERIFY", bool(parsed[1]), parsed[2]
    return None


def poll_reply(thread_id: str):
    pending = _pending(thread_id)
    if not pending:
        _reconcile(thread_id)
        pending = _pending(thread_id)
    for msg_id, text, _channel, _root in pending:
        if text.strip():
            return msg_id, text.strip()
        mark_processed(msg_id)
    return None


def mark_processed(message_id: str) -> None:
    with _database() as db:
        db.execute("UPDATE messages SET handled=1 WHERE id=?", (message_id,))
        db.execute("UPDATE status_requests SET handled=1 WHERE id=?", (message_id,))
        db.execute("UPDATE kick_requests SET handled=1 WHERE id=?", (message_id,))


def scrub_message(message_id: str) -> None:
    """Replace secure links in a handled message's stored copy. Never raises."""
    try:
        import secret_store
        with _database() as db:
            row = db.execute("SELECT text FROM messages WHERE id=?", (message_id,)).fetchone()
            if row:
                db.execute("UPDATE messages SET text=? WHERE id=?",
                           (secret_store.scrub(row[0]), message_id))
    except Exception:  # noqa: BLE001
        log.warning("could not scrub stored Slack message %s", message_id, exc_info=True)


def drain_thread(thread_id: str) -> int:
    pending = _pending(thread_id)
    for msg_id, _text, _channel, _root in pending:
        mark_processed(msg_id)
    return len(pending)
