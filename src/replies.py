"""One intent classifier for every reply coderbot waits on.

Commands, option numbers and keyword replies are resolved deterministically before
this is reached; only free text costs one utility call. Each wait names the actions it
accepts, and the reply is classified against exactly those, with one shared prompt
and one validation, so an unknown answer can never become an unintended action.
"""
from string import Template

import agent_runner
import handoffs
from prompts import fenced

RULES = {
    "approve": '"approve" ONLY when the reply is a clear, explicit go-ahead to implement the proposal as-is.',
    "changes": '"changes" when they request any modification; put the substance in "feedback". Choose '
               '"changes" even if they also ask to close/withdraw the PR as part of those changes.',
    "wait": '"wait" when they ask to keep waiting, defer approval, or say not to implement until approval '
            'without requesting substantive proposal changes. Existing approval boundaries are controller '
            'state, not missing requirements to add to artifacts. Do not classify \'no implementes hasta mi '
            'aprobación\' as a proposal revision. If they also request actual scope/design changes, use '
            '"changes" instead.',
    "merge": 'Merging is IRREVERSIBLE: "merge" ONLY when the reply is a clear, explicit instruction to merge '
             'the PR as-is. An unconditional user instruction to merge means "merge anyway": set "force": true, '
             'including plain "merge". Honor explicit conditions instead of treating a conditional, negated or '
             'hypothetical merge as unconditional authorization.',
    "retry": '"retry" when they want codebot to simply try the failed step again (e.g. "retry", "try again").',
    "instructions": '"instructions" when they provide guidance on HOW to fix or proceed; put it in "feedback".',
    "complete": '"complete" ONLY when the reply explicitly says the WHOLE TASK should be marked done with no '
                'further work and without codebot merging (e.g. "this is already implemented, mark it complete '
                'and move on"). A reply that merely answers a question or confirms a sub-step is not "complete".',
    "abort": '"abort" ONLY when the reply explicitly tells codebot to stop, abandon or reset the task WITHOUT '
             'marking it done (e.g. "abort", "cancel this", "never mind, stop working on this").',
    "resume_implementation": '"resume_implementation" ONLY when the pending question says verification is '
        'blocked by unfinished implementation tasks AND the reply explicitly authorizes completing those pending '
        'tasks within the ALREADY approved spec/scope (e.g. "Continue the pending implementation tasks of the '
        'approved spec; preserve existing work and do not replan"). A bare "yes", "continue", "retry", a VERIFY '
        'request, a request only to reconcile evidence, or new requirements are NOT this action.',
    "answer": '"answer" for everything else: answers, guidance, or new instructions for the current task, '
              'even ambiguous ones.',
    "send_link": '"send_link": they want to provide it or asked for the link.',
    "skip": '"skip": they cannot or will not provide it and want the work to continue without it.',
    "pause": '"pause": they want to wait or decide later.',
    "other": '"other": a question, a different instruction, or anything unclear.',
    "unclear": '"unclear" when the reply asks a question, is ambiguous, or you cannot confidently tell.',
}
# Per wait: the situation, the accepted actions (the last is the safe default).
KINDS = {
    "approval": ("The user replied to the proposal-review message.",
                 ("approve", "changes", "wait", "complete", "abort", "unclear")),
    "pr": ("The user replied to the pull-request review message.",
           ("merge", "changes", "complete", "abort", "unclear")),
    "stuck": ("Codebot got stuck on a task, messaged the user for help, and is waiting.",
              ("retry", "abort", "instructions", "complete", "unclear")),
    "credential": ("Codebot needs the credential $env ($purpose) to test against $service, and offered the "
                   "user three options: 1) send it through a secure one-time link, 2) continue without it "
                   "(checks that need it are skipped), 3) pause until they decide.",
                   ("send_link", "skip", "pause", "other")),
    "question": ("Codebot asked the user a question while working on a task and is waiting for the answer.",
                 ("answer", "complete", "abort", "resume_implementation")),
}
DEFAULTS = {"approval": "unclear", "pr": "unclear", "stuck": "unclear", "credential": "other",
            "question": "answer"}


def prompt(kind: str, reply: str, **fields) -> str:
    situation, actions = KINDS[kind]
    choices = " | ".join(f'"{action}"' for action in actions)
    question = fields.pop("question", None)
    return (Template(situation).safe_substitute(fields) + "\n\n"
            + (fenced("codebot's question", question) + "\n\n" if question is not None else "")
            + fenced("user reply", reply) + "\n\n"
            "Classify the reply's intent. Respond with ONLY a JSON object, no other text:\n"
            f'{{"action": {choices}, "feedback": "<their requested changes or instructions, empty otherwise>"'
            + (', "force": false' if "merge" in actions else "") + "}\n\n"
            + "\n".join("- " + RULES[action] for action in actions)
            + f'\nWhen in doubt, choose "{DEFAULTS[kind]}" rather than guessing.')


def classify(kind: str, reply: str, state: dict | None = None, **fields) -> dict:
    """The reply's intent among the actions `kind` accepts; anything else is the default."""
    import main
    text = prompt(kind, reply, **fields) + (handoffs.classification_context(state) if state else "")
    verdict = main.parse_json_reply(agent_runner.run(text, contract=False).output)
    if not isinstance(verdict, dict) or verdict.get("action") not in KINDS[kind][1]:
        verdict = {"action": DEFAULTS[kind]}
    return verdict
