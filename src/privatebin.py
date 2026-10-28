"""PrivateBin v2 links: parse, fetch once, decrypt locally.

A PrivateBin link looks like ``https://host/path/?<pasteid>#<base58 key>``. The server only
ever holds ciphertext; the key travels in the URL fragment, which browsers never send. The
bot fetches the paste JSON (``GET <base>?pasteid=<id>`` with ``X-Requested-With:
JSONHttpRequest``) and decrypts it here:

    key   = base58(fragment), left-padded with zero bytes to 32
    aes   = PBKDF2-HMAC-SHA256(key, salt, iterations, 32 bytes)
    plain = AES-256-GCM(aes, iv, ct[:-16], tag=ct[-16:], aad=json(adata, no spaces))
    text  = json(inflate_raw(plain) if compression == "zlib" else plain)["paste"]

Only what the bot needs is accepted: protocol v2, plain text (no attachments, no
discussion), burn-after-reading, bounded sizes. Anything else is ``LinkUnusable``.

Protocol reference: PrivateBin js/privatebin.js (CryptTool) and its JSON API docs.
"""
import base64
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

import config
import turn_control

MAX_CIPHERTEXT = 100_000      # bytes of paste JSON fetched
MAX_PLAINTEXT = 32_768        # bytes after decompression
FETCH_TIMEOUT = 20
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_PASTE_ID = re.compile(r"[0-9a-f]{16}")
_FRAGMENT = re.compile(r"-?[1-9A-HJ-NP-Za-km-z]{20,50}")
# A PrivateBin-shaped link anywhere in a message (the host is checked separately).
LINK_RE = re.compile(r"https?://[^\s<>|?#]+\?[0-9a-f]{16}#-?[1-9A-HJ-NP-Za-km-z]{20,50}")


class LinkUnusable(Exception):
    """The link or paste cannot be used: wrong host, malformed, expired, already read,
    unsupported options or a failed decryption. The message is safe to show the user."""


def b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        index = _B58.find(char)
        if index < 0:
            raise ValueError("not base58")
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


def base_of(url: str) -> str:
    """Scheme, host and path of an instance URL, normalised for comparison."""
    parsed = urllib.parse.urlsplit(url.strip())
    path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
    return f"{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}{'' if parsed.port in (None, 443, 80) else ':' + str(parsed.port)}{path}"


def parse_link(url: str, accepted: dict[str, str]) -> tuple[str, bytes]:
    """Map a user-supplied link to (fetch_url, key).

    ``accepted`` maps a public instance base (as ``base_of`` returns it) to the base the
    bot should fetch from; for the bot's own temporary instance that is the local
    container address, so the paste never round-trips through the public tunnel.
    """
    parsed = urllib.parse.urlsplit(url.strip())
    base = base_of(url)
    if (base not in accepted or parsed.scheme not in ("https", "http") or parsed.username
            or parsed.password or not _PASTE_ID.fullmatch(parsed.query or "")
            or not _FRAGMENT.fullmatch(parsed.fragment or "")):
        raise LinkUnusable("That link is not from the secure page I set up (or an allowed "
                           "PrivateBin instance), or it is incomplete.")
    if parsed.scheme == "http" and not accepted[base].startswith("http://127.0.0.1"):
        raise LinkUnusable("Only HTTPS PrivateBin links are accepted.")
    key = b58decode(parsed.fragment.removeprefix("-"))
    if len(key) > 32:
        raise LinkUnusable("The link's key is malformed.")
    return f"{accepted[base]}?pasteid={parsed.query}", key.rjust(32, b"\0")


def fetch(fetch_url: str, *, opener=None) -> dict:
    """GET the paste JSON once. No redirects, bounded size and time."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise LinkUnusable("The PrivateBin server redirected the request; refused.")

    opener = opener or urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(fetch_url, headers={
        "X-Requested-With": "JSONHttpRequest", "Accept": "application/json", "User-Agent": "codebot"})
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT) as response:
            body = response.read(MAX_CIPHERTEXT + 1)
    except LinkUnusable:
        raise
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise LinkUnusable(f"Could not reach the PrivateBin page ({type(error).__name__}).") from None
    if len(body) > MAX_CIPHERTEXT:
        raise LinkUnusable("The paste is larger than a credential should be.")
    try:
        payload = json.loads(body)
    except ValueError:
        raise LinkUnusable("The PrivateBin page did not return a paste.") from None
    if not isinstance(payload, dict) or payload.get("status", 0) != 0:
        raise LinkUnusable("That paste no longer exists: it was already read or it expired.")
    return payload


def decode(payload: dict, key: bytes) -> str:
    """Decrypt a v2 paste. Raises LinkUnusable on anything unexpected."""
    try:
        if payload.get("v") != 2:
            raise ValueError("version")
        adata = payload["adata"]
        if not isinstance(adata, list) or len(adata) != 4:
            raise ValueError("adata")
        spec, formatter, discussion, burn = adata
        if formatter != "plaintext" and formatter not in ("syntaxhighlighting", "markdown"):
            raise ValueError("formatter")
        if discussion != 0 or burn != 1:
            raise LinkUnusable("Create the paste with 'Burn after reading' on and discussions off.")
        if (not isinstance(spec, list) or len(spec) != 8 or type(spec[2]) is not int
                or not 10_000 <= spec[2] <= 1_000_000 or spec[3:7] != [256, 128, "aes", "gcm"]
                or spec[7] not in ("none", "zlib")):
            raise ValueError("cipher spec")
        iv = base64.b64decode(spec[0], validate=True)
        salt = base64.b64decode(spec[1], validate=True)
        ciphertext = base64.b64decode(payload["ct"], validate=True)
        if len(iv) < 12 or len(salt) < 8 or not 16 < len(ciphertext) <= MAX_CIPHERTEXT:
            raise ValueError("sizes")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # lazy: import never fails
        derived = hashlib.pbkdf2_hmac("sha256", key, salt, spec[2], dklen=32)
        aad = json.dumps(adata, separators=(",", ":"), ensure_ascii=False).encode()
        plain = AESGCM(derived).decrypt(iv, ciphertext, aad)
        if spec[7] == "zlib":
            inflater = zlib.decompressobj(-zlib.MAX_WBITS)
            plain = inflater.decompress(plain, MAX_PLAINTEXT + 1)
            if len(plain) > MAX_PLAINTEXT or not inflater.eof:
                raise ValueError("decompressed size")
        elif len(plain) > MAX_PLAINTEXT:
            raise ValueError("size")
        content = json.loads(plain)
        if not isinstance(content, dict) or "attachment" in content:
            raise LinkUnusable("Paste only the secret text, without attachments.")
        text = content.get("paste")
        if not isinstance(text, str) or not text.strip() or "\0" in text:
            raise ValueError("empty")
        return text.strip()
    except LinkUnusable:
        raise
    except Exception:
        raise LinkUnusable("I could not decrypt that paste (wrong or incomplete link, or an "
                           "unsupported PrivateBin version).") from None


# ---------------------------------------------------------------- temporary instance
#
# The bot can host its own short-lived PrivateBin: a container from the pinned image,
# reachable by the user through a localhost.run SSH tunnel (no account, no inbound
# port). Inside Docker the container joins the bot's network namespace
# (--network container:<bot>) and its nginx is moved to LOCAL_PORT, so 127.0.0.1:LOCAL_PORT
# is the same address for the bot's fetch and for the tunnel. Outside Docker it publishes
# 127.0.0.1:LOCAL_PORT instead.
# Every subprocess has a timeout; only start() raises (Unavailable).


CONTAINER_PORT = 8080   # the image's nginx default
LOCAL_PORT = 48080      # where the bot reaches the page: a host port outside Docker, or the
                        # page's own listen port when it shares the bot's network namespace
                        # (8080 would collide with the target app's dev servers)
LABEL = "codebot.privatebin"
TUNNEL_HOST = "nokey@localhost.run"
_TUNNEL_URL = re.compile(r"https://[a-z0-9][a-z0-9-]*\.lhr\.life\b")
_processes: dict[int, subprocess.Popen] = {}

CONF = """;<?php http_response_code(403); /*
; Temporary PrivateBin for handing one credential to codebot. Generated; do not edit.
[main]
name = "codebot secure drop"
discussion = false
opendiscussion = false
password = false
fileupload = false
burnafterreadingselected = true
defaultformatter = "plaintext"
sizelimit = 32768
qrcode = false
email = false
[expire]
default = "{default}"
[expire_options]
5min = 300
10min = 600
1hour = 3600
[formatter_options]
plaintext = "Plain Text"
[traffic]
limit = 5
[purge]
limit = 0
batchsize = 10
[model]
class = Filesystem
[model_options]
dir = PATH "data"
;*/
"""


class Unavailable(Exception):
    """The temporary instance could not be started; the message is safe to show."""


def _in_container() -> bool:
    return os.path.exists("/.dockerenv")


def _run(args, timeout, check=True):
    return subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout, check=check)


def _port_busy(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _alive(pid) -> bool:
    """Our tunnel process is running. A pid from a previous run counts only if it is still
    an ssh to localhost.run: after a restart pids are reused, and "dead" is the safe answer."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    process = _processes.get(pid)
    if process is not None:
        return process.poll() is None
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return _is_tunnel(pid)


def _is_tunnel(pid: int) -> bool:
    """Whether a pid is still our ssh tunnel (Linux /proc first; ps where /proc is absent)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return b"localhost.run" in handle.read()
    except FileNotFoundError:
        if os.path.isdir("/proc/self"):
            return False  # Linux and no such process
    except OSError:
        return False
    try:
        command = _run(["ps", "-o", "command=", "-p", str(pid)], 5, check=False).stdout
    except Exception:  # noqa: BLE001
        return False
    return "localhost.run" in command


def start(instance_id: str, directory, *, ttl: int = 3600) -> dict:
    """Start a container and a public tunnel; return the session to persist.

    Raises Unavailable with a user-safe reason. Partial resources are removed on failure."""
    if not shutil.which("docker") or not shutil.which("ssh"):
        raise Unavailable("docker or ssh is not available where I run.")
    directory = os.fspath(directory)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    port = LOCAL_PORT
    if _port_busy(port):
        cleanup_stale(instance_id)  # a page left by a previous run is the usual culprit
        time.sleep(1)
        if _port_busy(port):
            raise Unavailable(f"local port {port} is already in use.")
    network = (["--network", f"container:{socket.gethostname()}"] if _in_container()
               else ["-p", f"127.0.0.1:{LOCAL_PORT}:{CONTAINER_PORT}"])
    session = {"container_id": None, "tunnel_pid": None, "public_url": None,
               "local_url": f"http://127.0.0.1:{port}/", "expires_at": time.time() + ttl}
    try:
        created = _run(["docker", "create", "--rm", "--label", f"{LABEL}={instance_id}",
                        *network, config.PRIVATEBIN_IMAGE], 180)
        session["container_id"] = created.stdout.strip()
        conf = os.path.join(directory, "conf.php")
        with open(conf, "w") as handle:
            handle.write(CONF.replace("{default}", "1hour" if ttl >= 3600 else "10min"))
        _run(["docker", "cp", conf, f"{session['container_id']}:/srv/cfg/conf.php"], 30)
        if _in_container():
            _listen_on(session["container_id"], directory, port)
        _run(["docker", "start", session["container_id"]], 30)
        deadline = time.time() + 30
        while not _port_busy(port):
            if time.time() > deadline:
                raise Unavailable("the secure page did not come up in time.")
            time.sleep(0.5)
        log_path = os.path.join(directory, "tunnel.log")
        with open(log_path, "w") as log_file:
            process = subprocess.Popen(
                ["ssh", "-T", "-o", "StrictHostKeyChecking=accept-new",
                 "-o", f"UserKnownHostsFile={os.path.join(directory, 'known_hosts')}",
                 "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ExitOnForwardFailure=yes",
                 "-o", "ServerAliveInterval=30", "-R", f"80:127.0.0.1:{port}", TUNNEL_HOST],
                stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
                start_new_session=True)
        _processes[process.pid] = process
        session["tunnel_pid"] = process.pid
        deadline = time.time() + 30
        while session["public_url"] is None:
            with open(log_path, errors="replace") as handle:
                found = _TUNNEL_URL.search(handle.read())
            if found:
                session["public_url"] = found.group(0) + "/"
            elif process.poll() is not None or time.time() > deadline:
                raise Unavailable("the public tunnel (localhost.run) could not be opened.")
            else:
                time.sleep(0.5)
        return session
    except Unavailable:
        stop(session)
        raise
    except (subprocess.SubprocessError, OSError) as error:
        stop(session)
        raise Unavailable(f"the secure page could not be started ({type(error).__name__}).") from None


def _listen_on(container_id: str, directory: str, port: int) -> None:
    """Move the (not yet started) container's nginx from CONTAINER_PORT to `port`."""
    site = os.path.join(directory, "site.conf")
    _run(["docker", "cp", f"{container_id}:/etc/nginx/http.d/site.conf", site], 30)
    with open(site) as handle:
        original = handle.read()
    moved = re.sub(rf"(listen\s+(?:\[::\]:)?){CONTAINER_PORT}\b", rf"\g<1>{port}", original)
    if moved == original:
        raise Unavailable("the secure page's web server could not be configured.")
    with open(site, "w") as handle:
        handle.write(moved)
    _run(["docker", "cp", site, f"{container_id}:/etc/nginx/http.d/site.conf"], 30)


def accepted_bases(session: dict | None, allowlist=()) -> dict[str, str]:
    """Public base -> fetch base for parse_link: our tunnel maps to the local container."""
    bases = {base_of(url): base_of(url) for url in allowlist if url.startswith("https://")}
    if session and session.get("public_url") and session.get("local_url"):
        bases[base_of(session["public_url"])] = session["local_url"]
    return bases


def running(session: dict | None) -> bool:
    return bool(session and session.get("public_url") and _alive(session.get("tunnel_pid"))
                and time.time() < session.get("expires_at", 0))


def stop(session: dict | None) -> None:
    """Remove the container and close the tunnel. Never raises."""
    if not session:
        return
    pid = session.get("tunnel_pid")
    try:
        if _alive(pid):
            turn_control.signal_group(pid, signal.SIGTERM) if pid in _processes else os.kill(pid, signal.SIGTERM)
            process = _processes.pop(pid, None)
            if process is not None:
                process.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass
    try:
        if session.get("container_id"):
            _run(["docker", "rm", "-f", session["container_id"]], 30, check=False)
    except Exception:  # noqa: BLE001
        pass


def cleanup_stale(instance_id: str) -> None:
    """Remove containers this instance left behind (e.g. after a crash). Never raises."""
    try:
        if not shutil.which("docker"):
            return
        ids = _run(["docker", "ps", "-aq", "--filter", f"label={LABEL}={instance_id}"], 10,
                   check=False).stdout.split()
        if ids:
            _run(["docker", "rm", "-f", *ids], 30, check=False)
    except Exception:  # noqa: BLE001
        pass
