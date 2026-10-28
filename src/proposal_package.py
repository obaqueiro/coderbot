"""Build a safe, offline OpenSpec review document from actual change artifacts."""
import hashlib
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import markdown

import config


_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SAFE_TAGS = {"p", "br", "hr", "pre", "code", "blockquote", "ul", "ol", "li",
              "strong", "em", "del", "table", "thead", "tbody", "tr", "th", "td",
              "h1", "h2", "h3", "h4", "h5", "h6", "a"}
_VOID = {"br", "hr"}


def collect(repo: Path, slug: str) -> dict[str, str]:
    """Whitelist only required change artifacts; never follow links outside the change."""
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise ValueError("invalid OpenSpec change slug")
    root = (repo / "openspec" / "changes" / slug).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"OpenSpec change missing: {slug}")
    return collect_directory(root)


def collect_directory(root: Path) -> dict[str, str]:
    """The same artifact inventory before and after controller-owned archival."""
    root = Path(root).resolve()
    paths = [root / name for name in ("proposal.md", "design.md", "tasks.md")]
    paths += sorted((root / "specs").rglob("spec.md")) if (root / "specs").is_dir() else []
    if len(paths) == 3:
        raise FileNotFoundError("OpenSpec change has no specs")
    files = {}
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise FileNotFoundError(f"invalid or missing OpenSpec artifact: {path.relative_to(root)}")
        files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return files


class _SafeMarkdown(HTMLParser):
    """Keep Markdown semantics while removing executable HTML and unsafe URLs."""

    def __init__(self, namespace: str):
        super().__init__(convert_charrefs=True)
        self.namespace = namespace
        self.parts: list[str] = []
        self.headings: list[tuple[str, str]] = []
        self.heading: tuple[str, str, list[str]] | None = None
        self.count = 0
        self.blocked = 0
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "iframe", "svg"):
            self.blocked += 1
            return
        if tag == "img" and not self.blocked:
            self.parts.append(f"[Image: {html.escape(dict(attrs).get('alt') or 'image')}]")
            return
        if self.blocked or tag not in _SAFE_TAGS:
            return
        if tag.startswith("h") and len(tag) == 2 and tag[1] in "123456":
            self.count += 1
            anchor = f"{self.namespace}-h{self.count}"
            self.heading = (tag, anchor, [])
            self.parts.append(f'<{tag} id="{anchor}">')
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            parsed = urlsplit(href)
            if (parsed.scheme in ("https", "http", "mailto") or
                    (not parsed.scheme and not parsed.netloc and not href.startswith("//"))):
                self.parts.append(f'<a href="{html.escape(href, quote=True)}">')
                self.stack.append("a")
            else:
                self.parts.append("<span>")
                self.stack.append("span")
        else:
            self.parts.append(f"<{tag}>")
            if tag not in _VOID:
                self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "iframe", "svg"):
            self.blocked = max(0, self.blocked - 1)
            return
        if self.blocked or tag not in _SAFE_TAGS or tag in _VOID:
            return
        if self.heading and tag == self.heading[0]:
            self.headings.append((self.heading[1], "".join(self.heading[2]).strip()))
            self.heading = None
            self.parts.append(f"</{tag}>")
        elif self.stack and (tag == self.stack[-1] or tag == "a" and self.stack[-1] == "span"):
            self.parts.append(f"</{self.stack.pop()}>")

    def handle_data(self, data):
        if not self.blocked:
            self.parts.append(html.escape(data))
            if self.heading:
                self.heading[2].append(data)


_CSS = """body{margin:0;font:16px/1.6 system-ui,sans-serif;color:#182233;background:#f8fafc}
nav{position:fixed;top:0;bottom:0;left:0;width:250px;overflow:auto;padding:24px;background:#10213d;color:white}
nav a{display:block;color:#dce9fc;text-decoration:none;padding:4px 0;overflow-wrap:anywhere}
nav a:hover,nav a:focus{color:#fff;text-decoration:underline}nav .heading{padding-left:14px;font-size:13px}
nav input{width:95%;margin:12px 0;padding:8px}main{max-width:900px;margin-left:320px;padding:32px}
section{padding:16px 28px;margin-bottom:24px;background:white;border-radius:10px;box-shadow:0 1px 4px #cbd5e1}
pre{overflow:auto;background:#f1f5f9;padding:16px}code{overflow-wrap:anywhere}
table{border-collapse:collapse}td,th{border:1px solid #cbd5e1;padding:7px}
@media(max-width:750px){nav{position:static;width:auto;max-height:300px}main{margin:0;padding:12px}}"""
_JS = """document.getElementById('filter').addEventListener('input',function(){
const q=this.value.toLowerCase();document.querySelectorAll('nav a').forEach(function(a){
a.hidden=!a.textContent.toLowerCase().includes(q)})});"""


def render(files: dict[str, str], title: str) -> str:
    links, sections = [], []
    for index, (name, content) in enumerate(files.items()):
        namespace = f"doc-{index}"
        safe = _SafeMarkdown(namespace)
        safe.feed(markdown.markdown(content, extensions=["fenced_code", "tables", "sane_lists"]))
        safe.close()
        links.append(f'<a href="#{namespace}">{html.escape(name)}</a>')
        links.extend(f'<a class="heading" href="#{anchor}">{html.escape(label)}</a>'
                     for anchor, label in safe.headings)
        sections.append(f'<section id="{namespace}"><h2>{html.escape(name)}</h2>'
                        + "".join(safe.parts) + "</section>")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>'
            '<nav aria-label="Document index"><strong>OpenSpec</strong>'
            '<label for="filter">Find section</label><input id="filter" type="search" '
            'placeholder="Filter index">' + "".join(links) + '</nav><main>'
            f'<h1>{html.escape(title)}</h1>' + "".join(sections) + '</main>'
            f'<script>{_JS}</script></body></html>')


def fingerprint(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:16]


def scope_fingerprint(files: dict[str, str]) -> str:
    """Approval binds requirements/design/task text, not completion checkboxes."""
    from task_phases import TASK
    normalized = {name: TASK.sub(lambda m: m[1] + " " + m[3] + m[4], text) if name == "tasks.md" else text
                  for name, text in files.items()}
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()


def prepare(data_dir: Path, repo: Path, slug: str, branch: str,
            max_bytes: int) -> tuple[Path, dict[str, str]]:
    """Generate a frozen, complete attachment outside the working tree."""
    files = collect(repo, slug)
    document = render(files, f"OpenSpec proposal: {slug}")
    data = document.encode("utf-8")
    if len(data) > max_bytes:
        raise ValueError(f"OpenSpec review HTML is {len(data)} bytes, over the "
                         f"{max_bytes}-byte attachment cap")
    safe_branch = re.sub(r"[^a-zA-Z0-9._-]", "-", branch)
    dest = data_dir / "review_packages" / safe_branch
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"proposal-{fingerprint(files)}.html"
    if not target.exists():
        temp = target.with_suffix(".tmp")
        temp.write_bytes(data)
        temp.replace(target)
    return target, files


def save_snapshot(data_dir: Path, branch: str, files: dict[str, str]) -> Path:
    safe_branch = re.sub(r"[^a-zA-Z0-9._-]", "-", branch)
    dest = data_dir / "review_packages" / safe_branch
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"proposal-{fingerprint(files)}.json"
    if not target.exists():
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(files, ensure_ascii=False), encoding="utf-8")
        temp.replace(target)
    return target


def previous(path: str | None) -> dict[str, str] | None:
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else None


def changes(before: dict[str, str] | None, after: dict[str, str]) -> str:
    if before is None:
        return "Prior sent proposal snapshot unavailable; review the complete attached package."
    lines = []
    for path in sorted(before.keys() | after.keys()):
        if path not in before:
            lines.append(f"Added: {path}")
        elif path not in after:
            lines.append(f"Removed: {path}")
        elif before[path] != after[path]:
            old, new = _sections(before[path]), _sections(after[path])
            for heading in sorted(old.keys() | new.keys()):
                if heading not in old:
                    lines.append(f"Added in {path}: {heading}")
                elif heading not in new:
                    lines.append(f"Removed from {path}: {heading}")
                elif old[heading] != new[heading]:
                    lines.append(f"Changed in {path}: {heading}")
    return "\n".join(f"- {item}" for item in lines) if lines else "No artifact changes in this revision."


def _sections(text: str) -> dict[str, str]:
    """Heading-level deterministic diff, including task bodies and requirements."""
    result: dict[str, str] = {}
    key = "(introduction)"
    for line in text.splitlines(keepends=True):
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            key = match.group(1)
            # Repeated headings are legal; never overwrite an earlier section.
            if key in result:
                key += f" ({sum(k.startswith(key) for k in result) + 1})"
        result[key] = result.get(key, "") + line
    return result
