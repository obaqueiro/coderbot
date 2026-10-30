"""A fake GitHub and checkout under lifecycle.RepoOps, for repo-mode unit tests: PRs pinned
by number, pushes recorded, the local HEAD and tracked changes settable."""
from contextlib import ExitStack
import json
from unittest.mock import patch

import errors
import lifecycle

SLUG = "acme/project"


class FakeGitHub:
    def __init__(self, head="a" * 40, base="main"):
        self.local_head, self.base, self.dirty = head, base, ""
        self.prs, self.calls, self.pushes = {}, [], []
        self.fail_push = self.fail_merge = None
        self.canonical = {}  # number -> the URL GitHub now reports (a renamed or transferred repository)

    def url(self, number):
        return f"https://github.com/{SLUG}/pull/{number}"

    def add(self, number, branch, **extra):
        self.prs[number] = {"head": branch, "base": self.base, "state": "OPEN", "draft": False, "sha": self.local_head,
                            "cross": False, "body": "", "title": "", "mergeable": "MERGEABLE", **extra}
        return self.url(number)

    def push(self, ops, branch):
        if self.fail_push:
            raise self.fail_push
        self.pushes.append(branch)
        for pr in self.prs.values():
            if pr["head"] == branch and pr["state"] == "OPEN":
                pr["sha"] = self.local_head

    def gh(self, ops, *args):
        self.calls.append(args)
        option = lambda name: args[args.index(name) + 1]
        if args[:2] == ("pr", "list"):
            return json.dumps([{"number": n, "url": self.url(n)} for n, pr in self.prs.items()
                               if pr["head"] == option("--head") and pr["state"] == "OPEN"])
        if args[:2] == ("pr", "create"):
            number = max(self.prs, default=41) + 1
            self.add(number, option("--head"), draft="--draft" in args, body=option("--body"), title=option("--title"))
            return self.url(number)
        if int(args[2]) not in self.prs:
            raise errors.AgentFault(f"no pull request found for #{args[2]}")
        pr = self.prs[int(args[2])]
        if args[:2] == ("pr", "view"):
            url = self.canonical.get(int(args[2])) or (ops.url(args[2]) if ops.remote else self.url(args[2]))
            value = {"number": int(args[2]), "url": url, "state": pr["state"], "isDraft": pr["draft"],
                     "mergeable": pr["mergeable"], "headRefName": pr["head"], "baseRefName": pr["base"],
                     "headRefOid": pr["sha"], "isCrossRepository": pr["cross"], "statusCheckRollup": [],
                     "body": pr["body"]}
            return json.dumps({key: value[key] for key in option("--json").split(",")})
        if args[:2] == ("pr", "ready"):
            pr["draft"] = False
        elif args[:2] == ("pr", "edit"):
            pr.update(body=option("--body"), title=option("--title") if "--title" in args else pr["title"])
        elif args[:2] == ("pr", "merge"):
            if self.fail_merge:
                raise errors.AgentFault(self.fail_merge)
            pr["state"] = "MERGED"
        return ""

    def __enter__(self):
        self.stack = ExitStack()
        for name, value in (("gh", lambda ops, *args: self.gh(ops, *args)),
                            ("push", lambda ops, branch: self.push(ops, branch)),
                            ("head", lambda ops: self.local_head),
                            ("tracked_changes", lambda ops: self.dirty)):
            self.stack.enter_context(patch.object(lifecycle.RepoOps, name, value))
        return self

    def __exit__(self, *exc):
        self.stack.close()
