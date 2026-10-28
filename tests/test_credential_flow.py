"""The credential request flow in main: ask, receive through PrivateBin, skip, pause, recover."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import config
import redaction
import secret_store

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main

LINK = "https://abc123def.lhr.life/?0123456789abcdef#" + "7" * 44
REQUEST = 'CREDENTIAL_REQUEST: {"env":"STRIPE_KEY","service":"Stripe","purpose":"live refund test"}'
SESSION = {"container_id": "c1", "tunnel_pid": 99, "public_url": "https://abc123def.lhr.life/",
           "local_url": "http://127.0.0.1:8080/", "expires_at": time.time() + 3600}


def result(output=""):
    return SimpleNamespace(output=output, session_id="sid", question=None, attachments=[], preamble="")


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patches = [patch.object(config, "DATA_DIR", Path(tmp.name)),
                   patch.object(config, "CREDENTIAL_REQUESTS", True),
                   patch.object(config, "COMM_CHANNEL", "email"),
                   patch.object(main, "save_state"), patch.object(main, "trail"),
                   patch.object(main, "_transcript_append"), patch.object(main, "_transcript_note"),
                   patch.object(main, "_note_contact")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.email = patch.object(main, "email").start()
        self.addCleanup(patch.stopall)
        redaction.clear()
        self.addCleanup(redaction.clear)
        self.addCleanup(secret_store._captured.clear)
        self.addCleanup(secret_store.activate, None)

    def waiting(self, **extra):
        state = {"state": "WAIT_REPLY", "item": "task", "slug": "s", "branch": "b", "thread_id": "T",
                 "session_id": "sid", "return_state": "IMPLEMENTING", "pending_question": "q",
                 "credential_request": {"env": "STRIPE_KEY", "service": "Stripe", "purpose": "p",
                                        "optional": True, "phase": "IMPLEMENTING", "session": None,
                                        "question": "q"}}
        state.update(extra)
        secret_store.activate(main._credential_key(state))
        return state

    def reply(self, state, text, msg="m1"):
        """Run handle_wait with one incoming reply; returns (queue, answer, processed) mocks."""
        captured = secret_store.capture(msg, text)
        order = []
        with patch.object(main.gmail_client, "poll_reply", return_value=(msg, captured)), \
                patch.object(main.gmail_client, "foreign_command", return_value=None), \
                patch.object(main.gmail_client, "mark_processed", side_effect=lambda m: order.append("processed")) as processed, \
                patch.object(main, "_queue_conversation", return_value=True) as queue, \
                patch.object(main, "_answer_question", side_effect=lambda s, r: order.append(("answer", r))) as answer:
            main.handle_wait(state)
        return queue, answer, processed, order


class Asking(Base):
    def test_request_pauses_in_wait_reply_with_options(self):
        state = {"state": "IMPLEMENTING", "item": "task", "branch": "b"}
        self.assertTrue(main.handle_result(state, result("done\n" + REQUEST), "IMPLEMENTING"))
        self.assertEqual(state["state"], "WAIT_REPLY")
        self.assertEqual(state["return_state"], "IMPLEMENTING")
        self.assertEqual(state["credential_request"]["env"], "STRIPE_KEY")
        subject, body = self.email.call_args.args[1:3]
        self.assertEqual(subject, "credential needed")
        self.assertIn("1. Send it through a secure one-time link", body)

    def test_feature_off_skipped_or_present_credentials_do_not_pause(self):
        with patch.object(config, "CREDENTIAL_REQUESTS", False):
            state = {"state": "IMPLEMENTING", "item": "task"}
            self.assertFalse(main.handle_result(state, result(REQUEST), "IMPLEMENTING"))
        state = {"state": "IMPLEMENTING", "item": "task", "credentials": {"STRIPE_KEY": {"decision": "skip"}}}
        self.assertFalse(main.handle_result(state, result(REQUEST), "IMPLEMENTING"))
        with patch.dict("os.environ", {"STRIPE_KEY": "already-set"}):
            state = {"state": "IMPLEMENTING", "item": "task"}
            self.assertFalse(main.handle_result(state, result(REQUEST), "IMPLEMENTING"))
        self.email.assert_not_called()

    def test_failed_email_leaves_the_phase_untouched(self):
        self.email.side_effect = RuntimeError("slack down")
        state = {"state": "IMPLEMENTING", "item": "task"}
        self.assertFalse(main.handle_result(state, result(REQUEST), "IMPLEMENTING"))
        self.assertEqual(state["state"], "IMPLEMENTING")
        self.assertNotIn("credential_request", state)

    def test_agent_is_told_about_requests_only_when_enabled(self):
        import claude_runner
        self.assertIn("credential {env, service, purpose}", claude_runner.contract_text())
        with patch.object(config, "CREDENTIAL_REQUESTS", False):
            self.assertNotIn("credential {env", claude_runner.contract_text())

    def test_skipped_credentials_relax_requested_checks(self):
        plan = {"version": 1, "checks": [{"id": "live", "argv": ["pytest"], "env_keys": ["STRIPE_KEY"]}]}
        state = {"state": "VERIFYING", "item": "task", "credentials": {"STRIPE_KEY": {"decision": "skip"}}}
        with patch.object(main.checks, "execute_plan", return_value=[]) as run, \
                patch.object(main.phase_checkpoint, "store"):
            main.handle_result(state, result("CHECK_PLAN: " + json.dumps(plan)), "VERIFYING")
        self.assertTrue(run.call_args.args[0][0].optional)


class Replies(Base):
    def test_option_one_opens_a_page_without_queueing_the_reply(self):
        state = self.waiting()
        with patch.object(main.privatebin, "start", return_value=dict(SESSION)) as start:
            queue, answer, processed, _ = self.reply(state, "1")
        start.assert_called_once()
        queue.assert_not_called()
        processed.assert_called_once()
        self.assertEqual(state["credential_request"]["session"]["public_url"], SESSION["public_url"])
        self.assertEqual(self.email.call_args.args[1], "credential link")
        self.assertIn(SESSION["public_url"], self.email.call_args.args[2])
        self.assertEqual(state["state"], "WAIT_REPLY")

    def test_link_is_stored_before_the_message_is_consumed_and_never_echoed(self):
        state = self.waiting()
        state["credential_request"]["session"] = dict(SESSION)
        with patch.object(main.privatebin, "fetch", return_value={"v": 2}), \
                patch.object(main.privatebin, "decode", return_value="sk_live_secret_value"), \
                patch.object(main.privatebin, "stop") as stop:
            queue, answer, processed, order = self.reply(state, f"here: {LINK}")
        queue.assert_not_called()
        self.assertEqual(order[-1], "processed")
        resumed = order[0][1]
        self.assertIn("STRIPE_KEY", resumed)
        self.assertNotIn("sk_live_secret_value", resumed)
        self.assertEqual(secret_store.values(main._credential_key(state)), {"STRIPE_KEY": "sk_live_secret_value"})
        self.assertEqual(state["credentials"]["STRIPE_KEY"]["decision"], "provided")
        self.assertNotIn("credential_request", state)
        stop.assert_called()
        self.assertNotIn("sk_live_secret_value", json.dumps(state))
        self.assertEqual(redaction.redact("sk_live_secret_value"), "[REDACTED]")

    def test_failed_resume_is_retried_on_the_next_poll_without_stranding_the_user(self):
        state = self.waiting()
        state["credential_request"]["session"] = dict(SESSION)
        calls = []

        def flaky(s, message):
            calls.append(message)
            if len(calls) == 1:
                raise ConnectionResetError("transient")
        with patch.object(main.privatebin, "fetch", return_value={"v": 2}), \
                patch.object(main.privatebin, "decode", return_value="sk_live_secret_value"), \
                patch.object(main.privatebin, "stop"), \
                patch.object(main.gmail_client, "foreign_command", return_value=None), \
                patch.object(main, "_queue_conversation", return_value=True) as queue, \
                patch.object(main, "_answer_question", side_effect=flaky):
            for _ in range(2):
                captured = secret_store.capture("m1", f"here: {LINK}")
                with patch.object(main.gmail_client, "poll_reply", return_value=("m1", captured)), \
                        patch.object(main.gmail_client, "mark_processed") as processed:
                    try:
                        main.handle_wait(state)
                    except ConnectionResetError:
                        processed.assert_not_called()  # the message stays available
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        queue.assert_not_called()
        self.assertNotIn("secure link not used", [c.args[1] for c in self.email.call_args_list])
        self.assertNotIn("credential_request", state)

    def test_a_new_request_from_the_resumed_turn_survives(self):
        state = self.waiting()

        def asks_again(s, message):
            s["credential_request"] = {"env": "OTHER_KEY", "service": "x", "purpose": "y", "session": None}
        with patch.object(main, "_answer_question", side_effect=asks_again):
            main._credential_skip(state)
        self.assertEqual(state["credential_request"]["env"], "OTHER_KEY")

    def test_commands_keep_their_meaning_while_a_request_is_pending(self):
        state = self.waiting()
        for text in ("HOLD", "PAUSE", "ABORT", "STATUS", "retry", "CONTINUE"):
            with self.subTest(text=text):
                self.assertIsNone(main._credential_action(state, "m", text))

    def test_unusable_link_keeps_waiting_and_explains(self):
        state = self.waiting()
        queue, answer, processed, _ = self.reply(state, f"{LINK}")  # no session: host not accepted
        answer.assert_not_called()
        self.assertEqual(self.email.call_args.args[1], "credential not received")
        self.assertEqual(state["state"], "WAIT_REPLY")
        self.assertIn("credential_request", state)

    def test_natural_language_skip_and_pause(self):
        for text in ("2", "sigue sin la clave por favor", "continue without it"):
            with self.subTest(text=text):
                state = self.waiting()
                queue, answer, _, _ = self.reply(state, text)
                answer.assert_called_once()
                self.assertEqual(state["credentials"]["STRIPE_KEY"]["decision"], "skip")
        for text in ("3", "espera, luego te digo"):
            with self.subTest(text=text):
                state = self.waiting()
                queue, answer, _, _ = self.reply(state, text)
                answer.assert_not_called()
                self.assertEqual(self.email.call_args.args[1], "credential paused")
                self.assertIn("credential_request", state)

    def test_prose_goes_through_the_model_and_other_falls_back_to_the_normal_path(self):
        state = self.waiting()
        long_question = "¿Para qué exactamente necesitas esa credencial y qué permisos debería tener la cuenta? " * 2
        with patch.object(main.agent_runner, "run", return_value=SimpleNamespace(output='{"action": "other"}')):
            queue, answer, _, _ = self.reply(state, long_question)
        queue.assert_called_once()
        with patch.object(main.agent_runner, "run", return_value=SimpleNamespace(output='{"action": "skip"}')):
            queue, answer, _, _ = self.reply(self.waiting(), long_question)
        queue.assert_not_called()
        answer.assert_called_once()

    def test_classifier_failure_falls_back_to_the_normal_path(self):
        state = self.waiting()
        with patch.object(main.agent_runner, "run", side_effect=RuntimeError("model down")):
            queue, answer, _, _ = self.reply(state, "x" * 200)
        queue.assert_called_once()

    def test_page_unavailable_still_allows_continuing_without_it(self):
        state = self.waiting()
        with patch.object(main.privatebin, "start", side_effect=main.privatebin.Unavailable("docker or ssh is not available where I run.")):
            self.reply(state, "1")
        self.assertEqual(self.email.call_args.args[1], "credential page unavailable")
        self.assertIn("reply 2", self.email.call_args.args[2])
        queue, answer, _, _ = self.reply(state, "2", msg="m2")
        answer.assert_called_once()

    def test_unexpected_link_is_not_opened(self):
        state = self.waiting()
        state.pop("credential_request")
        with patch.object(main.privatebin, "fetch") as fetch, \
                patch.object(main, "_queue_conversation", return_value=True) as queue:
            captured = secret_store.capture("m1", f"approved, and the key is {LINK}")
            with patch.object(main.gmail_client, "poll_reply", return_value=("m1", captured)), \
                    patch.object(main.gmail_client, "foreign_command", return_value=None), \
                    patch.object(main.gmail_client, "mark_processed"):
                main.handle_wait(state)
        fetch.assert_not_called()
        queue.assert_called_once()  # the rest of the message still takes today's path
        self.assertEqual(queue.call_args.args[2], "approved, and the key is [secure-link]")
        self.assertEqual(self.email.call_args.args[1], "secure link not used")

    def test_a_superseded_request_does_not_capture_answers_to_the_next_question(self):
        state = self.waiting(pending_question="Which refund policy? 1. Full 2. Partial")
        state["credential_request"]["session"] = dict(SESSION)
        with patch.object(main.privatebin, "start") as start, patch.object(main.privatebin, "stop") as stop:
            queue, answer, _, _ = self.reply(state, "1")
        start.assert_not_called()
        stop.assert_called_once()
        queue.assert_called_once()
        self.assertNotIn("credential_request", state)


class Housekeeping(Base):
    def test_tick_drops_orphan_requests_and_closes_dead_pages(self):
        state = self.waiting(state="IMPLEMENTING")
        with patch.object(main.privatebin, "stop") as stop:
            main._credential_tick(state)
        self.assertNotIn("credential_request", state)
        stop.assert_called_once()
        state = self.waiting()
        state["credential_request"]["session"] = dict(SESSION)
        with patch.object(main.privatebin, "running", return_value=False), \
                patch.object(main.privatebin, "stop"):
            main._credential_tick(state)
        self.assertIsNone(state["credential_request"]["session"])
        self.assertEqual(self.email.call_args.args[1], "credential page closed")

    def test_tick_never_raises(self):
        with patch.object(main.secret_store, "activate", side_effect=RuntimeError("boom")):
            main._credential_tick({"item": "t"})

    def test_task_end_drops_values_hold_keeps_them(self):
        state = self.waiting()
        key = main._credential_key(state)
        secret_store.put(key, "STRIPE_KEY", "value-123456", ttl=60, source="t")
        with patch.object(main.privatebin, "stop"):
            main._release_credentials(state, drop=False)
            self.assertEqual(secret_store.values(key), {"STRIPE_KEY": "value-123456"})
            main._release_credentials(state, drop=True)
        self.assertEqual(secret_store.values(key), {})

    def test_startup_cleanup_spares_a_live_page(self):
        with patch.object(main, "load_state", return_value={"credential_request": {"session": dict(SESSION)}}), \
                patch.object(main.privatebin, "running", return_value=True), \
                patch.object(main.privatebin, "cleanup_stale") as cleanup:
            main._startup_credentials()
        cleanup.assert_not_called()
        with patch.object(main, "load_state", return_value={}), \
                patch.object(main.privatebin, "cleanup_stale") as cleanup:
            main._startup_credentials()
        cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
