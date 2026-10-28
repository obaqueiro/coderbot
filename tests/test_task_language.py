"""The backlog task, not subsequent chat, determines working and outgoing language."""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import agent_runner
import config
import prompts

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(),
                              "gmail_client": Mock()}):
    import main


class TaskLanguageTests(unittest.TestCase):
    def test_translation_timeout_preserves_report_and_uses_cached_fallback(self):
        state = {'task_language': 'Spanish'}
        with patch.object(main.agent_runner, 'run', side_effect=TimeoutError('slow')) as run:
            first = main._localized_pair(state, 'PR ready for review', 'Original verified report')
            second = main._localized_pair(state, 'PR ready for review', 'Original verified report')
        self.assertEqual(first, ('PR listo para revisión', 'Original verified report'))
        self.assertEqual(second, first)
        self.assertEqual(run.call_count, 1)
    def tearDown(self):
        agent_runner.set_task_language(None)

    def test_classifies_title_and_detail_once_and_persists_result(self):
        state = {"item": "Como usuario quiero elegir un modelo",
                 "item_detail": "- El selector debe persistir por sesión",
                 "state": "EXPLORING"}
        with patch.object(main.agent_runner, "run", return_value=SimpleNamespace(
                output='{"language":"Spanish"}')) as run, \
             patch.object(main, "save_state") as saved:
            main._ensure_task_language(state)
            main._ensure_task_language(state)
        self.assertEqual(state["task_language"], "Spanish")
        saved.assert_called_once_with(state)
        run.assert_called_once()
        self.assertFalse(run.call_args.kwargs["contract"])
        self.assertIn(state["item"], run.call_args.args[0])
        self.assertIn(state["item_detail"], run.call_args.args[0])

    def test_detection_failure_never_raises_and_falls_back_to_english(self):
        # Detection runs before mailbox commands every tick; raising here would make
        # ABORT unreachable for as long as the utility call keeps failing.
        state = {"item": "Exportar CSV", "state": "EXPLORING"}
        with patch.object(main.agent_runner, "run", side_effect=TimeoutError("slow")) as run, \
             patch.object(main, "save_state"):
            for _ in range(main.TASK_LANGUAGE_ATTEMPTS - 1):
                main._ensure_task_language(state)
                self.assertNotIn("task_language", state)
            main._ensure_task_language(state)
            main._ensure_task_language(state)
        self.assertEqual(state["task_language"], "English")
        self.assertNotIn("task_language_failures", state)
        self.assertEqual(run.call_count, main.TASK_LANGUAGE_ATTEMPTS)

    def test_accepts_non_ascii_language_names(self):
        state = {"item": "Eksporter CSV", "state": "EXPLORING"}
        with patch.object(main.agent_runner, "run", return_value=SimpleNamespace(
                output='{"language":"Norwegian Bokmål"}')), patch.object(main, "save_state"):
            main._ensure_task_language(state)
        self.assertEqual(state["task_language"], "Norwegian Bokmål")

    def test_working_sessions_and_openspec_keep_ticket_language(self):
        agent_runner.set_task_language("Spanish")
        with patch.object(config, "AGENT", "opencode"), \
             patch.object(agent_runner.phase_checkpoint, "replay", return_value=None), \
             patch.object(agent_runner.phase_checkpoint, "record"), \
             patch.object(agent_runner, "_opencode", return_value=Mock()) as invoke:
            agent_runner.run(prompts.PROPOSE)
            self.assertIn("Use Spanish", invoke.call_args.args[0])
            self.assertIn("all OpenSpec artifacts", invoke.call_args.args[0])
            self.assertIn("specs including requirement/scenario text", invoke.call_args.args[0])
            agent_runner.resume("ses_one", prompts.ANSWER_REPLY)
            self.assertIn("Use Spanish", invoke.call_args.args[0])
            self.assertEqual(invoke.call_args.args[1], "ses_one")
            agent_runner.run(prompts.CLASSIFY_QUESTION_REPLY, contract=False)
            self.assertNotIn("Use Spanish", invoke.call_args.args[0])

    def test_restart_applies_persisted_language_before_dispatching_reply(self):
        state = {"state": "WAIT_REPLY", "item": "Seleccionar modelo",
                 "task_language": "Spanish", "thread_id": "C123:1.0"}
        with patch.object(main, "load_state", return_value=state), \
             patch.object(main, "_announce_state"), \
             patch.object(main, "_ensure_task_language") as classify, \
             patch.object(main, "check_commands", side_effect=SystemExit("tick intercepted")):
            with self.assertRaisesRegex(SystemExit, "tick intercepted"):
                main._run_loop()
        classify.assert_called_once_with(state)
        self.assertIn("Use Spanish", agent_runner._language_prompt("continue"))

    def test_sends_user_message_and_subject_in_language_of_ticket(self):
        state: dict = {"item": "Seleccionar modelo", "slug": "modelo", "state": "WAIT_REPLY",
                       "thread_id": "C123:1.0", "task_language": "Spanish"}
        with patch.object(main.agent_runner, "run", side_effect=[
                SimpleNamespace(output='{"subject":"pregunta durante EXPLORING","body":"Tarea: Seleccionar modelo\\n\\nResponde en este hilo."}')]) as run, \
             patch.object(main.gmail_client, "send", return_value="C123:1.0") as send, \
             patch.object(main, "trail"), patch.object(main, "_note_contact"):
            main.email(state, "question during EXPLORING",
                       "Task: Seleccionar modelo\n\nReply in this Slack thread.")
        self.assertEqual(run.call_count, 1)
        self.assertTrue(all(c.kwargs["contract"] is False for c in run.call_args_list))
        self.assertIn("pregunta durante EXPLORING", send.call_args.args[0])
        self.assertEqual(send.call_args.args[1], "Tarea: Seleccionar modelo\n\nResponde en este hilo.")
        self.assertEqual(state["last_email"]["body"], send.call_args.args[1])

    def test_english_task_does_not_add_translation_call(self):
        with patch.object(main.agent_runner, "run") as run:
            self.assertEqual(main._localized({"task_language": "English"}, "Task: hello"),
                             "Task: hello")
        run.assert_not_called()

    def test_activity_trail_uses_task_language_too(self):
        state = {"item": "Mejoras de modelos", "task_language": "Spanish"}
        with patch.object(main.config, "ACTIVITY_TRAIL", True), \
             patch.object(main, "_localized", return_value="[bot] Tarea seleccionada") as localize, \
             patch.object(main.task_source, "note_activity") as note:
            main.trail(state, "Picked this item", "working on branch")
        localize.assert_called_once_with(
            state, f"[{config.INSTANCE_ID}] Picked this item\n\nworking on branch")
        self.assertEqual(note.call_args.args[2], "[bot] Tarea seleccionada")


if __name__ == "__main__":
    unittest.main()
