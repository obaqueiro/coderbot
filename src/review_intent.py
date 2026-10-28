"""Reviewer provenance and explicit approved-intent framing."""
def provenance(thread):
    author = thread.get("author", "")
    body = thread.get("body", "")
    kind = thread.get("author_type", "")
    if kind == "Bot" or author.endswith("[bot]") or "ai-review-inline" in body:
        return "automated"
    if kind == "User":
        return "human"
    return "unknown"


RULES = """
Evaluate every finding against approved OpenSpec requirements and actual behavior.
Classify it as defect, clarification, preference or design conflict. Fix real defects;
explain justified nonchanges. Do not reverse approved defaults solely to close threads.
If compatibility evidence invalidates a material approved assumption, ask for replanning.
Reviewer provenance does not prove correctness. Preserve verification repair commits.
"""
