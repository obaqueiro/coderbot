"""Coderbot's fixed messages are translated to Spanish from a catalog, never by a model."""
import ast
import re
import unittest
from pathlib import Path

import message_templates

BILINGUAL = re.compile(r"[áéíóñ¿]|provider-blocked|Keep paused|Mantener")  # already chosen by language
SENDERS = {"email", "_send_localized", "_enter_stuck"}
BODIES = {"body", "decision", "question", "message", "detail", "text", "intro", "note", "reason"}
NOT_USER_TEXT = {", so ", "Resolved by codebot.", "* picked: ",
                 "No hay un turno del agente en ejecución; la tarea sigue igual.",
                 "Reiniciando este turno del agente; conservaré la tarea y el trabajo ya guardado."}


def fragments():
    tree = ast.parse(Path("src/main.py").read_text())
    def literals(node):
        return [x.value for x in ast.walk(node) if isinstance(x, ast.Constant) and isinstance(x.value, str)
                and " " in x.value.strip() and any(c.isalpha() for c in x.value)]
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in SENDERS:
            found.update(literal for arg in node.args[1:3] for literal in literals(arg))
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AugAssign) else []
        if any(isinstance(t, ast.Name) and t.id in BODIES for t in targets):
            found.update(literals(node.value))
    tree = ast.parse(Path("src/handoffs.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "title" for t in node.targets):
            found.update(literals(node.value))
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_option" and node.args:
            found.update(literals(node.args[0]))
    return {f for f in found - NOT_USER_TEXT if not BILINGUAL.search(f)}


class Catalog(unittest.TestCase):
    def test_every_fixed_message_fragment_has_a_spanish_entry(self):
        missing = sorted(f for f in fragments() if f not in message_templates.PHRASES)
        self.assertEqual(missing, [], "add these to message_templates.PHRASES")

    def test_values_between_fragments_pass_through(self):
        text = message_templates.translate(
            "I did not merge: the PR has 2 unresolved review thread(s).\nPR: https://x/1", "Spanish")
        self.assertEqual(text, "No hice merge: el PR tiene 2 hilo(s) de revisión sin resolver.\nPR: https://x/1")
        self.assertEqual(message_templates.translate("Implementation · 2/3", "spanish"), "Implementación · 2/3")
        self.assertEqual(message_templates.translate("Implementación del agente", "Spanish"), "Implementación del agente")

    def test_english_is_unchanged_and_other_languages_need_a_model(self):
        self.assertEqual(message_templates.translate("merge blocked", "English"), "merge blocked")
        self.assertIsNone(message_templates.translate("merge blocked", "French"))


if __name__ == "__main__":
    unittest.main()
