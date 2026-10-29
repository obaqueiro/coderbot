"""The lifecycle over a list of repositories: Target{root, repos} plus RepoOps(repo).

Workspace mode drives it today; single-repo is the N=1 case (`Target.of` a repo-mode
context) and keeps its own handlers in main.py until it is ported behind golden tests.
RepoOps is the only place that runs git/gh for a repository: every call is time-bounded,
failures are typed (errors.py), PRs are pinned by number, and the controller alone
fetches and pushes. Per-repository records live in state["workspace"]["repositories"]."""
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import subprocess
import time

import config
import errors
import operations

log = logging.getLogger("coderbot")
PR_FIELDS = "number,url,state,isDraft,mergeable,headRefName,baseRefName,headRefOid,isCrossRepository"
FATAL_TOOL_ERROR = re.compile(r"authentication failed|could not read username|permission denied \(publickey\)|"
                              r"bad credentials|http 401|gh auth login|not logged into", re.IGNORECASE)
GREEN = {"SUCCESS", "NEUTRAL", "SKIPPED", "COMPLETED", "EXPECTED"}


def run(argv, cwd, *, timeout=None):
    """stdout of a bounded command; a failure is Transient, Fatal or AgentFault by its cause."""
    process = call(argv, cwd, timeout=timeout)
    if process.returncode:
        import diagnostics
        text = diagnostics.redact(f"{' '.join(argv[:2])} failed in {cwd}: {process.stderr}{process.stdout}".strip())
        kind = (errors.Fatal if FATAL_TOOL_ERROR.search(text) else
                errors.Transient if errors.TRANSIENT_TOOL_ERROR.search(text) else errors.AgentFault)
        raise kind(text, code=argv[0] + "_failed", resource=str(cwd))
    return process.stdout.strip()


def call(argv, cwd, *, timeout=None):
    """The finished process, any exit status, of a bounded operations.tool call."""
    label = " ".join(argv[:2])
    try:
        return operations.tool(argv, cwd=cwd, timeout=timeout or config.SUBPROCESS_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, TimeoutError) as error:
        raise errors.Transient(f"{label} timed out in {cwd}", code=argv[0] + "_timeout", resource=str(cwd)) from error
    except OSError as error:
        raise errors.Fatal(f"{label} cannot run: {error}", code=argv[0] + "_missing", resource=str(cwd)) from error


def pr_number(record):
    """The pinned PR number; older records only stored the URL."""
    if record.get("pr_number"):
        return int(record["pr_number"])
    match = re.search(r"/pull/(\d+)$", record.get("pr_url") or "")
    return int(match[1]) if match else None


@dataclass(frozen=True)
class RepoOps:
    path: Path
    name: str
    remote: str
    base: str
    infer: bool = False  # repo mode: any GitHub host; before a PR exists gh resolves the checkout

    @classmethod
    def checkout(cls, path, base, pr_url=None):
        """The repository of repo mode, named by its PR once there is one."""
        match = re.match(r"https://([^/]+/[^/]+/[^/]+)/pull/\d+$", pr_url or "")
        return cls(Path(path), ".", match[1] if match else "", base, infer=True)

    @property
    def slug(self):
        return self.remote.split("/", 1)[-1] if self.infer else self.remote.removeprefix("github.com/")

    def git(self, *args, timeout=None):
        return run(["git", *args], self.path, timeout=timeout)

    def quiet(self, *args):
        """git output, or "" when the command fails (probes such as rev-parse --verify)."""
        try:
            return self.git(*args)
        except errors.CoderbotError:
            return ""

    def _gh(self, args):
        if args[0] == "api":  # endpoints name the repository themselves
            return ["gh", *args]
        if self.infer:
            return ["gh", *args, *(["--repo", self.remote] if self.remote else [])]
        if not self.remote.startswith("github.com/"):
            raise errors.Fatal(f"PR publication requires a GitHub origin: {self.name}", code="not_github")
        return ["gh", *args, "--repo", self.slug]

    def gh(self, *args):
        return run(self._gh(args), self.path)

    def gh_quiet(self, *args):
        """The finished gh process, or None when it timed out or could not start: for
        queries that fail open, or read stdout on a non-zero exit (pr checks)."""
        try:
            return call(self._gh(args), self.path)
        except errors.CoderbotError as error:
            log.warning("gh %s could not complete: %s", " ".join(args[:2]), error)
            return None

    def branch(self):
        return self.quiet("branch", "--show-current")

    def head(self):
        return self.git("rev-parse", "HEAD")

    def tracked_changes(self):
        return self.git("status", "--porcelain", "--untracked-files=no")

    def clean(self):
        return not self.quiet("status", "--porcelain", "--untracked-files=all")

    def fetch(self, ref=None):
        """Best effort: an unreachable origin leaves the last fetched refs in place."""
        try:
            self.git("fetch", "--quiet", "origin", ref or self.base, timeout=max(config.SUBPROCESS_TIMEOUT_SECONDS, 120))
            return True
        except errors.CoderbotError as error:
            log.warning("fetch %s failed in %s: %s", ref or self.base, self.name, error)
            return False

    def base_sha(self, head="HEAD"):
        """merge-base of `head` and the freshly fetched origin base (the PR's real base)."""
        self.fetch()
        upstream = (self.quiet("rev-parse", "--verify", f"refs/remotes/origin/{self.base}^{{commit}}")
                    or self.quiet("rev-parse", "--verify", f"refs/heads/{self.base}^{{commit}}"))
        return (self.quiet("merge-base", head, upstream) if upstream else "") or upstream or self.quiet("rev-parse", head)

    def ahead(self, base_sha):
        return bool(base_sha) and int(self.quiet("rev-list", "--count", f"{base_sha}..HEAD") or 0) > 0

    def is_ancestor(self, older, newer):
        try:
            self.git("merge-base", "--is-ancestor", older, newer)
            return True
        except errors.CoderbotError:
            return False

    def push(self, branch):
        self.git("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", timeout=max(config.SUBPROCESS_TIMEOUT_SECONDS, 300))

    def pr(self, number, fields=PR_FIELDS):
        return json.loads(self.gh("pr", "view", str(number), "--json", fields))

    def with_pr(self, url):
        """Repo mode learns which repository it is from the PR it just opened."""
        return RepoOps.checkout(self.path, self.base, url) if self.infer else self

    def url(self, number):
        return f"https://{self.remote if self.infer else 'github.com/' + self.slug}/pull/{number}"

    def open_pr(self, branch, title, body, *, draft):
        """The open PR of this attempt's unique branch, else a new one: (number, url)."""
        output = self.gh("pr", "list", "--state", "open", "--head", branch, "--base", self.base,
                         "--json", "number,url", "--limit", "2")
        try:
            listed = json.loads(output or "[]")
        except ValueError:
            listed = None
        if (not isinstance(listed, list) or len(listed) > 1 or any(
                not isinstance(item, dict) or not isinstance(item.get("number"), int)
                or not str(item.get("url") or "https://").startswith("https://") for item in listed)):
            raise errors.Fatal(f"{self.name}: gh pr list returned an invalid or ambiguous result for {branch}",
                               code="ambiguous_pr")
        if listed:
            return listed[0]["number"], listed[0].get("url") or self.url(listed[0]["number"])
        url = (self.gh("pr", "create", *(["--draft"] if draft else []), "--base", self.base, "--head", branch,
                       "--title", title, "--body", body).strip().splitlines() or [""])[-1]
        if not re.fullmatch(r"https://\S+/pull/\d+", url):
            raise errors.AgentFault(f"{self.name}: gh pr create printed no HTTPS PR URL: {url[:200]!r}", code="pr_create")
        return pr_number({"pr_url": url}), url

    def matches(self, record, value):
        """The PR is the one this record pinned: number, branches and repository (any case)."""
        expected = self.url(pr_number(record)).rsplit("/", 1)[0].casefold() + "/"
        return (isinstance(value, dict) and value.get("number") == pr_number(record)
                and str(value.get("url", "")).casefold().startswith(expected)
                and value.get("headRefName") == record.get("branch") and value.get("baseRefName") == self.base
                and value.get("isCrossRepository") is False)


@dataclass(frozen=True)
class Target:
    root: Path
    repos: tuple[RepoOps, ...]

    @classmethod
    def of(cls, context):
        return cls(context.root, tuple(RepoOps(r.path, r.relative_path, r.remote, r.base_branch)
                                       for r in context.repositories))

    def repo(self, name):
        for ops in self.repos:
            if ops.name == name:
                return ops
        raise errors.Fatal(f"Not a discovered repository: {name}", code="missing_repository")


def on_branch(ops, record):
    """Put the repository back on its feature branch (after a hold/resume); never forces."""
    branch = record.get("branch")
    if not branch or ops.branch() == branch:
        return
    if not ops.quiet("rev-parse", "--verify", f"refs/heads/{branch}"):
        return
    if not ops.clean():
        raise errors.Fatal(f"{ops.name} has uncommitted changes on {ops.branch() or 'a detached HEAD'}; "
                           f"save them before I switch it back to {branch}", code="dirty_repository")
    ops.git("checkout", "--quiet", branch)


def adopt(ops, record, before):
    """Move work the agent made on the base branch onto this attempt's feature branch."""
    current = ops.branch()
    if current != ops.base or not record.get("branch") or ops.quiet("rev-parse", "--verify", f"refs/heads/{record['branch']}"):
        return
    ops.git("checkout", "--quiet", "-b", record["branch"])
    if before.get("branch") == ops.base and before.get("head") and ops.quiet("rev-parse", ops.base) != before["head"]:
        ops.git("branch", "--force", ops.base, before["head"])  # local commits now live on the feature branch


def release(target, records):
    """Return each clean child to its base branch so the next task starts from it."""
    for name, record in records.items():
        try:
            ops = target.repo(name)
            if ops.branch() != ops.base and ops.clean():
                ops.git("checkout", "--quiet", ops.base)
                if ops.fetch():
                    ops.quiet("merge", "--ff-only", "--quiet", f"origin/{ops.base}")
        except errors.CoderbotError as error:
            log.warning("could not return %s to %s: %s", name, record.get("base_branch"), error)


def open_drafts(target, records, title, body, save):
    """A draft PR per repository on its first commit past merge-base; best effort.
    Returns the names that got a PR now (the caller announces and cross-links them)."""
    opened = []
    for name, record in records.items():
        if pr_number(record) or record.get("merge", {}).get("status") == "MERGED" or record.get("draft_attempts", 0) >= 2:
            continue
        ops = target.repo(name)
        try:
            if not ops.ahead(record.get("base_sha")):
                continue
            ops.push(record["branch"])
            number, url = ops.open_pr(record["branch"], title(), body(name), draft=True)
        except Exception as error:  # noqa: BLE001 - the ready step opens it at the end
            record["draft_attempts"] = record.get("draft_attempts", 0) + 1
            log.warning("draft PR for %s failed (attempt %d): %s", name, record["draft_attempts"], error)
            continue
        record.update(pr_number=number, pr_url=url, draft=True)
        save()
        opened.append(name)
    return opened


def push_progress(target, records):
    """Best-effort progress push to every open draft; the next turn retries a failure.
    A ready PR only receives verified heads (the publish step)."""
    for name, record in records.items():
        if pr_number(record) and record.get("draft") and record.get("merge", {}).get("status") != "MERGED":
            try:
                target.repo(name).push(record["branch"])
            except Exception as error:  # noqa: BLE001
                log.warning("progress push for %s failed: %s", name, error)


def progress(target, records, title, body, save):
    """The rails after a turn: push to open drafts, then open the missing ones; never raises.
    Returns the names that got a draft PR now."""
    try:
        push_progress(target, records)
        return open_drafts(target, records, title, body, save)
    except Exception as error:  # noqa: BLE001 - never fail the turn; OPEN_PR publishes
        log.warning("progress publication failed: %s", error)
        return []


def edit_bodies(target, records, body):
    for name, record in records.items():
        if pr_number(record) and record.get("merge", {}).get("status") not in ("MERGED", "CLOSED"):
            try:
                target.repo(name).gh("pr", "edit", str(pr_number(record)), "--body", body(name))
            except Exception as error:  # noqa: BLE001 - cross-links are informational
                log.warning("could not update the PR body for %s: %s", name, error)


def diff_summary(target, records, budget):
    """Per-repository diff stats for the reviewer, within a shared character budget."""
    names = [n for n, r in records.items() if r.get("base_sha")]
    share = max(400, budget // max(1, len(names)))
    parts = []
    for name in names:
        record = records[name]
        stat = target.repo(name).quiet("diff", "--stat", f"{record['base_sha']}...HEAD")
        if len(stat) > share:
            stat = stat[:share] + "\n… (truncated; run the diff yourself)"
        parts.append(f"- {name}: base {record['base_sha']}, branch {record.get('branch')}\n{stat}")
    return "\n".join(parts)


def merge_order(records, declared):
    names = [n for n in records if pr_number(records[n])]
    return [n for n in declared if n in names] + [n for n in names if n not in declared]


def green(value):
    rollup = value.get("statusCheckRollup") or []
    return all((c.get("conclusion") or c.get("state") or "").upper() in GREEN for c in rollup)


def precheck(target, records, order):
    """Every PR still to merge is open, mergeable, green and at its verified head."""
    problems = []
    for name in order:
        record = records[name]
        if record.get("merge", {}).get("status") == "MERGED":
            continue
        value = target.repo(name).pr(pr_number(record), PR_FIELDS + ",statusCheckRollup")
        if value.get("state") == "MERGED":  # merged before a crash could record it
            record["merge"] = {"status": "MERGED"}
            continue
        if value.get("state") != "OPEN":
            problems.append(f"{name}: PR is {value.get('state')}")
        elif value.get("headRefOid") != record.get("delivered_sha"):
            problems.append(f"{name}: PR head moved since verification")
        elif value.get("mergeable") != "MERGEABLE":
            problems.append(f"{name}: not mergeable ({value.get('mergeable')})")
        elif not green(value):
            problems.append(f"{name}: checks are not green")
        elif value.get("isDraft"):
            problems.append(f"{name}: PR is still a draft")
    return problems


def merge(target, records, order, save):
    """Merge in order; returns the report (merged, failed, error, order)."""
    report = {"order": order, "merged": [], "failed": None, "error": None}
    for name in order:
        record = records[name]
        if record.get("merge", {}).get("status") == "MERGED":
            report["merged"].append(name)
            continue
        try:
            # Pinned to the verified head; a legacy repo-mode record without one merges as it always did.
            pin = ["--match-head-commit", record["delivered_sha"]] if record.get("delivered_sha") else []
            target.repo(name).gh("pr", "merge", str(pr_number(record)), "--squash", *pin)
        except errors.CoderbotError as error:
            report.update(failed=name, error=str(error)[:500])
            break
        record["merge"] = {"status": "MERGED"}
        report["merged"].append(name)
        save()
    return report


# ---- handlers over a target; `ws` adapts the mode (workspace_runner today)
def rails(ws, state):
    """After each implementation turn: progress pushes and lazy draft PRs (best effort)."""
    import main
    if not config.DRAFT_PR:
        return
    try:
        ops = Target.of(ws.target())
    except Exception as error:  # noqa: BLE001 - never fail the turn; OPEN_PR publishes
        log.warning("workspace progress publication failed: %s", error)
        return
    records = state["workspace"]["repositories"]
    opened = progress(ops, records, lambda: main._pr_title(state), lambda name: pr_body(state, name, draft=True),
                      lambda: main.save_state(state))
    if opened:
        edit_bodies(ops, records, lambda name: pr_body(state, name, draft=True))
        main.email(state, "draft PR", "I opened draft PRs so you can follow the implementation; I push after "
                   "each step and mark them ready once the feature is verified and reviewed.\n\n" + status_body(state))


def threads_by_repo(review_data):
    """{repository: threads}; older states stored one repository's threads."""
    if review_data.get("repository") and review_data.get("threads"):
        return {review_data["repository"]: review_data["threads"]}
    return review_data.get("threads_by_repo") or {}


def status_body(state):
    records = state["workspace"]["repositories"]
    lines = ["Feature: " + state.get("item", "")]
    for name, record in records.items():
        if record.get("pr_url"):
            lines.append(f"- {name}: {record['pr_url']} ({record.get('merge', {}).get('status') or 'OPEN'})")
    edits = sorted({entry["path"] for entry in state["workspace"].get("local_edits", [])})
    if edits:
        lines += ["", "Local-only workspace changes (NOT delivered by merging PRs):", *["- " + p for p in edits]]
    concerns = state["workspace"].get("concerns") or []
    if concerns:
        lines += ["", "Review notes left open:", *["- " + c for c in concerns]]
    report = state["workspace"].get("merge_report")
    if report and report.get("failed"):
        lines += ["", f"Partial merge: merged {', '.join(report['merged']) or 'none'}; {report['failed']} failed: "
                  f"{report['error']}"]
    lines += ["", "Each repository PR is independent; the feature completes only after all required merges."]
    return "\n".join(lines)


def pr_body(state, name, *, draft=False):
    return (("Work in progress: coderbot pushes as it implements and marks this PR ready after verification "
             "and review.\n\n" if draft else "")
            + f"Feature: {state['item']}\nSource: {state.get('item_url') or 'backlog'}\nRepository: {name}\n"
              f"Shared local plan: {state.get('archive_path', state['slug'])}\n\n"
            + ("" if draft else state.get("implementation_summary", "") + "\n\n") + status_body(state))


def closed(state, name, record):
    """A PR closed without merging: ask, keeping every other repository's PR."""
    import main
    number = pr_number(record)
    record.setdefault("closed_prs", []).append(number)
    for key in ("pr_number", "pr_url", "delivered_sha", "announced", "draft"):
        record.pop(key, None)
    record["merge"] = {}
    main.save_state(state)
    main._enter_stuck(state, "PUSHING", f"The PR for {name} (#{number}) was closed without merging. Reply "
                      "'retry' to open a new PR for it from the same verified branch, 'complete' to finish the "
                      "task without it, or 'abort' to stop. The other repositories' PRs are unchanged.")


def moved(state, name, ops, record, value):
    """The PR head is not the verified head: take an external push in, or push ours."""
    import main
    on_branch(ops, record)
    remote, local = value.get("headRefOid"), ops.head()
    ops.fetch(record["branch"])
    if remote and remote != local and ops.is_ancestor(local, remote):
        ops.git("merge", "--ff-only", "--quiet", remote)
        main.email(state, "PR updated externally", f"New commits were pushed to the {name} PR; I took them in "
                   "and am re-verifying the feature before it can merge.")
        state["state"] = "VERIFYING"
    elif remote and ops.is_ancestor(remote, local):
        state["state"] = "PUSHING"
    else:
        main._enter_stuck(state, "WAIT_MERGE", f"The {name} PR head ({remote}) diverged from the verified branch "
                          f"({local}). Reconcile the branch by hand, then reply 'retry'.")


def pinned(ops, record):
    """The record's PR, refused unless it is still this repository's branch PR."""
    value = ops.pr(pr_number(record))
    if not ops.matches(record, value):
        raise errors.Fatal(f"{ops.name}: {record.get('pr_url')} no longer matches its repository and branch",
                           code="pr_mismatch")
    return value


def deliver(ops, record, title, body, save, *, ready_body=False):
    """Push the verified head to the record's PR (validated first, never forced), open one
    when there is none, mark a draft ready (`ready_body`: with the final title and body)
    and record the delivered head. A PR that is no longer open is returned untouched."""
    if pr_number(record):
        value = pinned(ops, record)
        if value["state"] != "OPEN":
            return value
    if ops.tracked_changes():
        raise errors.AgentFault(f"{ops.name}: PR publication requires committed tracked changes", code="dirty_publication")
    ops.push(record["branch"])
    if not pr_number(record):
        number, url = ops.open_pr(record["branch"], title(), body(), draft=False)
        record.update(pr_number=number, pr_url=url, draft=False)
        ops = ops.with_pr(url)
        save()  # persist accepted publication before any further call or notification
    value = pinned(ops, record)
    if value["state"] == "OPEN" and value["headRefOid"] != ops.head():
        raise errors.Transient(f"{ops.name}: the PR head does not match the pushed HEAD yet", code="pr_head_lag")
    if value["state"] == "OPEN" and (value.get("isDraft") or record.get("draft")):
        if ready_body:
            ops.gh("pr", "edit", str(value["number"]), "--title", title(), "--body", body())
        ops.gh("pr", "ready", str(value["number"]))
    record.update(pr_number=value["number"], delivered_sha=value["headRefOid"], draft=False)
    save()
    return value


def publish(ws, state):
    import main
    context = ws.target()
    if main._scope_drift(state) or main._conversation_delivery_blocked(state):
        return
    ws.bind(state, context)
    if state["workspace"].get("verified_snapshot") != ws.snapshot(context.root):
        state["state"] = "VERIFYING"
        return
    ops = Target.of(context)
    records = state["workspace"]["repositories"]
    for name, record in records.items():
        if record.get("merge", {}).get("status") == "MERGED":
            continue
        repo = ops.repo(name)
        on_branch(repo, record)
        if repo.tracked_changes():
            result = ws.turn(state, f"Commit the completed feature changes in {name}, excluding evidence and "
                             "secrets. Do not push; leave unrelated changes alone.")
            if not main.handle_result(state, result, "OPEN_PR"):
                state["state"] = "VERIFYING"
            return
        if not repo.ahead(record.get("base_sha")):
            record["state"] = "NO_CHANGES"
            continue
        value = deliver(repo, record, lambda: main._pr_title(state), lambda: pr_body(state, name),
                        lambda: main.save_state(state))
        if value["state"] == "CLOSED":
            return closed(state, name, record)
        record.update(state="WAIT_MERGE", merge={"status": value["state"]})
        main.save_state(state)
    resolve_threads(state)
    fresh = [name for name, record in records.items() if record.get("pr_url") and not record.get("announced")]
    state["state"] = "WAIT_MERGE"
    if fresh:
        edit_bodies(ops, records, lambda name: pr_body(state, name))
        reports = [entry["report"] for entry in state.get("final_check_report", {}).get("checks", [])
                   if entry.get("report") and Path(entry["report"]).is_file()]
        attachments = [Path(path) for path in dict.fromkeys([*reports, *state["workspace"].get("attachments", [])])
                       if Path(path).is_file()]
        main.email(state, "PRs ready for review", status_body(state), attachments)
        if state.get("last_delivery", {}).get("complete") is not False:
            for name in fresh:
                records[name]["announced"] = True
            main.save_state(state)
        for name in fresh:
            try:
                main.task_source.note_pr(state["item"], state.get("item_id"), records[name]["pr_url"])
            except Exception:
                log.exception("Could not link workspace PR on backlog")
    if not any(record.get("pr_url") for record in records.values()):
        main.email(state, "local workspace changes verified", status_body(state))
        if state.get("last_delivery", {}).get("complete") is not False:
            main._finish_task(state, "Verified local-only workspace changes.\n" + status_body(state), reset_repo=False)


def resolve_threads(state):
    """Reply to and resolve the threads the last ADDRESS_PR_THREADS turn answered."""
    import main
    output = state["workspace"].pop("review_output", None)
    review_data = state.pop("workspace_review", {})
    records = state["workspace"]["repositories"]
    for name, threads in threads_by_repo(review_data).items() if output else ():
        if name in records and records[name].get("pr_url"):
            reviewed = {"pr_url": records[name]["pr_url"], "pr_threads": threads,
                        "pr_threads_seen": records[name].setdefault("review", {}).get("seen", {})}
            main._resolve_review_threads(reviewed, output, key="pr_threads")
            records[name]["review"]["seen"] = reviewed.get("pr_threads_seen", {})


def poll_prs(ws, state):
    """One throttled poll per interval covering every repository's PR."""
    import main
    data = state["workspace"]
    if time.time() < data.get("pr_polled_at", 0) + config.POLL_INTERVAL_SECONDS:
        return main.handle_wait(state)
    data["pr_polled_at"] = time.time()
    context = ws.target()
    ops = Target.of(context)
    records = data["repositories"]
    pending, threads, conflicts = False, {}, []
    for name, record in records.items():
        number = pr_number(record)
        if not number:
            continue
        repo = ops.repo(name)
        value = repo.pr(number)
        if not repo.matches(record, value):
            raise errors.Fatal(f"{name}: {record.get('pr_url')} no longer matches its repository and branch", code="pr_mismatch")
        record.update(pr_number=number, merge={"status": value["state"], "mergeable": value.get("mergeable")})
        if value["state"] == "CLOSED":
            return closed(state, name, record)
        if value["state"] == "MERGED":
            continue
        pending = True
        if value.get("headRefOid") != record.get("delivered_sha"):
            return moved(state, name, repo, record, value)
        if value.get("mergeable") == "CONFLICTING":
            conflicts.append(name)
            continue
        listed = main.unresolved_review_threads(record["pr_url"])
        seen = record.setdefault("review", {}).get("seen", {})
        fresh = [thread for thread in listed or [] if main.thread_has_new_comments(thread, seen)]
        if fresh:
            threads[name] = fresh
    if conflicts:
        state["workspace_review"] = {"conflicts": conflicts}
        state["state"] = "RESOLVE_CONFLICTS"
        return
    if threads and data.get("thread_rounds", 0) < config.PR_THREAD_MAX_ROUNDS:
        data["thread_rounds"] = data.get("thread_rounds", 0) + 1
        state["workspace_review"] = {"threads_by_repo": threads}
        state["state"] = "ADDRESS_PR_THREADS"
        return
    if pending:
        return main.handle_wait(state)
    if any(record.get("pr_url") for record in records.values()):
        release(ops, records)  # children now sit on the merged bases
        main._finish_task(state, "All feature PRs merged.\n" + integrated_check(ws, state, context)
                          + status_body(state), reset_repo=False, merged=True)


def integrated_check(ws, state, context):
    """W7: the shared integration checks once more on the merged bases. Informational:
    the PRs are merged, so a failure is reported for a follow-up, never reverted."""
    import checks
    import main
    try:
        plan = [check for check in ws.check_plan_for(state, context) if check.cwd in (".", "e2e")]
        if not plan:
            return ""
        store = main.phase_checkpoint.store()
        results = checks.execute_plan(plan, context.root, store, store.task_identity(state, context.root))
    except Exception as error:  # noqa: BLE001 - best effort after the merge
        log.warning("integrated check after merge could not run: %s", error)
        return "Integrated check after merge could not run: " + str(error)[:300] + "\n"
    lines = [f"- {r.get('check')}: {r.get('status')}" for r in results]
    failed = any(r.get("status") not in ("pass", "skipped") for r in results)
    return ("Integrated check on the merged bases " + ("FAILED; a follow-up is needed:" if failed else "passed:")
            + "\n" + "\n".join(lines) + "\n")


def merge_all(ws, state, force=False):
    """Pre-check every PR, then merge in the manifest's order; record a partial merge."""
    import main
    context = ws.target()
    if state["workspace"].get("verified_snapshot") != ws.snapshot(context.root):
        state["state"] = "VERIFYING"
        return
    ops = Target.of(context)
    records = state["workspace"]["repositories"]
    order = merge_order(records, context.manifest.merge_order)
    problems = precheck(ops, records, order)
    for name in order:
        if records[name].get("merge", {}).get("status") != "MERGED":
            threads = main.unresolved_review_threads(records[name]["pr_url"])
            if threads is None or threads and not force:
                problems.append(f"{name}: review threads remain or could not be checked")
    if problems:
        main.email(state, "merge blocked", "Nothing was merged:\n" + "\n".join("- " + p for p in problems)
                   + "\n\n" + status_body(state))
        return
    report = merge(ops, records, order, lambda: main.save_state(state))
    state["workspace"]["merge_report"] = report
    main.save_state(state)
    if report["failed"]:
        main.email(state, "partial merge", f"Merged in order: {', '.join(report['merged']) or 'none'}. "
                   f"{report['failed']} failed: {report['error']}\nThe remaining PRs are untouched; reply 'merge' "
                   "to retry them once the cause is fixed.\n\n" + status_body(state))
        return
    state["workspace"]["pr_polled_at"] = 0
    poll_prs(ws, state)
