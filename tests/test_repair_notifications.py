"""Supervisor delivery uses the same durable contract for every channel adapter."""
import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import config
import conversation
import gmail_client
import repair_notifications
import slack_client
from execution_store import ExecutionStore
from scripts.selfheal import atomic_json


class RepairNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "supervisor/notifications"
        self.folder.mkdir(parents=True)
        self.store = ExecutionStore(self.root / "execution.sqlite")
        for target, value in (("config.DATA_DIR", self.root), ("config.REPO_PATH", self.root),
                              ("phase_checkpoint.store", lambda: self.store)):
            manager = patch(target, value)
            manager.start()
            self.addCleanup(manager.stop)

    def event(self, name="event", channel="email"):
        path = self.folder / (name + ".json")
        atomic_json(path, {"version": 1, "id": name, "kind": "repair_recovered", "task_id": "task",
                           "provider": "synthetic-backlog", "channel": channel, "conversation": "C:1",
                           "files": ["src/bug.py"]})
        return path

    def test_email_slack_and_future_adapter_share_delivery_contract(self):
        fixture = Mock(__name__="telegram_fixture")
        fixture.attachment_limits.return_value = {"file": 10000, "mime": False}
        for channel, backend in (("email", gmail_client), ("slack", slack_client), ("telegram-test", fixture)):
            with self.subTest(channel=channel):
                path = self.event(channel, channel)
                def send(envelope, receipts, confirm):
                    self.assertEqual(envelope["thread_id"], "C:1")
                    self.assertEqual(envelope["attachments"], [])
                    self.assertIn("src/bug.py", envelope["body"])
                    confirm("body", {"status": "confirmed"})
                    return "C:1"
                with patch.object(config, "COMM_CHANNEL", channel), \
                     patch.object(conversation, "_backend", return_value=backend), \
                     patch.object(backend, "send_envelope", side_effect=send) as sender:
                    repair_notifications.flush()
                    repair_notifications.flush()
                    sender.assert_called_once()
                self.assertFalse(path.exists())
                self.assertTrue(path.with_suffix(".delivered").exists())

    def test_transport_failure_stays_pending_then_retries_same_receipt(self):
        path = self.event()
        with patch.object(config, "COMM_CHANNEL", "email"), \
             patch.object(gmail_client, "send", side_effect=RuntimeError("definitely rejected")):
            repair_notifications.flush()
        self.assertTrue(path.exists())
        pending = self.store.list("delivery_receipt", "supervisor:task", status="pending")
        self.assertEqual(len(pending), 1)
        with patch.object(config, "COMM_CHANNEL", "email"), patch.object(gmail_client, "send", return_value="C:1") as sender:
            repair_notifications.flush()
        sender.assert_called_once()
        self.assertEqual(self.store.list("delivery_receipt", "supervisor:task")[0]["id"], pending[0]["id"])

    def test_acceptance_survives_crash_before_outbox_rename(self):
        path = self.event()
        with patch.object(config, "COMM_CHANNEL", "email"), \
             patch.object(gmail_client, "send", return_value="C:1") as sender:
            with patch.object(Path, "rename", side_effect=OSError("crash after acceptance")):
                repair_notifications.flush()
            self.assertTrue(path.exists())
            repair_notifications.flush()
            sender.assert_called_once()
        self.assertTrue(path.with_suffix(".delivered").exists())

    def test_one_broken_event_does_not_starve_other_events(self):
        (self.folder / "000-corrupt.json").write_text("invalid JSON")
        path = self.event()
        with patch.object(config, "COMM_CHANNEL", "email"), patch.object(gmail_client, "send", return_value="C:1"):
            repair_notifications.flush()
        self.assertFalse(path.exists())
        self.assertTrue((self.folder / "000-corrupt.json").exists())

    def test_channel_change_never_redirects_private_event(self):
        path = self.event(channel="slack")
        with patch.object(config, "COMM_CHANNEL", "email"), patch.object(conversation, "_backend") as backend:
            repair_notifications.flush()
        self.assertTrue(path.exists())
        backend.assert_not_called()

    def test_worker_and_bridge_cannot_send_concurrently(self):
        self.event()
        with (self.folder.parent / "notifications.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(repair_notifications, "_deliver") as sender:
                repair_notifications.flush()
            sender.assert_not_called()

    def test_missing_provider_credentials_stay_queued(self):
        path = self.event()
        with patch.object(config, "COMM_CHANNEL", "email"), patch.object(gmail_client, "send", side_effect=SystemExit("missing credentials")):
            repair_notifications.flush()
        self.assertTrue(path.exists())

    def test_interrupted_sender_rotates_attempts_without_starvation(self):
        first, second = self.event("first"), self.event("second")
        import os
        os.utime(first, (1, 1))
        os.utime(second, (2, 2))
        with patch.object(repair_notifications, "_deliver", side_effect=KeyboardInterrupt) as deliver:
            with self.assertRaises(KeyboardInterrupt):
                repair_notifications.flush()
            self.assertEqual(deliver.call_args.args[0], first)
        with patch.object(repair_notifications, "_deliver", side_effect=KeyboardInterrupt) as deliver:
            with self.assertRaises(KeyboardInterrupt):
                repair_notifications.flush()
            self.assertEqual(deliver.call_args.args[0], second)
