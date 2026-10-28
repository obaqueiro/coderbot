"""Safe, cross-process progress from Codebot and OpenCode's live SQLite WAL."""
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import activity
import agent_runner
import config


class DurableTurn(unittest.TestCase):
    def test_separate_reader_sees_sanitized_active_turn_and_completion(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "DATA_DIR", Path(tmp)):
            state = {"branch": "bot-feature", "state": "IMPLEMENTING"}
            activity.begin(state, "ses-parent")
            activity.observe(state, activity="task: Completar API", now=1000.0)
            recorded = activity.durable_turn({"branch": "bot-feature"})
            self.assertTrue(recorded["active"])
            self.assertEqual(recorded["session_id"], "ses-parent")
            self.assertEqual(recorded["activity"], "task: Completar API")
            self.assertEqual(recorded["last_activity"], 1000.0)
            activity.finish(state)
            self.assertFalse(activity.durable_turn(state)["active"])
            self.assertEqual(activity.durable_turn({"branch": "another"}), {})

    def test_old_process_is_not_reported_as_active(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "DATA_DIR", Path(tmp)):
            state = {"branch": "bot-feature", "state": "IMPLEMENTING"}
            activity.begin(state, "ses-parent")
            with patch.object(activity.os, "kill", side_effect=ProcessLookupError):
                result = activity.snapshot(state, {}, 200.0)
        self.assertFalse(result["active"])
        self.assertTrue(result["process_dead"])

    def test_agent_stream_persists_only_safe_metadata_across_process_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(config, "DATA_DIR", Path(tmp)), \
             patch.object(config, "AGENT", "claude"):
            state = {"branch": "bot-feature", "state": "IMPLEMENTING"}
            agent_runner.set_task_context(state)
            try:
                def fake_run(_prompt, contract=False):
                    agent_runner._stream_line(json.dumps({
                        "type": "tool_use", "part": {"tool": "task", "state": {"input": {
                            "description": "Completar proveedor API", "prompt": "PRIVATE PROMPT"}}}}))
                    self.assertEqual(activity.durable_turn(state)["activity"],
                                     "task: Completar proveedor API")
                    return object()
                with patch.object(agent_runner.claude_runner, "run", side_effect=fake_run):
                    agent_runner.run("SECRET PROMPT", contract=False)
                self.assertFalse(activity.durable_turn(state)["active"])
                with closing(sqlite3.connect(Path(tmp) / "agent_activity.sqlite")) as db:
                    stored = str(db.execute("SELECT * FROM turn").fetchone())
                self.assertNotIn("PRIVATE PROMPT", stored)
                self.assertNotIn("SECRET PROMPT", stored)
            finally:
                agent_runner.set_task_context(None)


class OpenCodeMetadata(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.xdg = self.root / "xdg"
        self.db_path = self.xdg / "opencode/opencode.db"
        self.db_path.parent.mkdir(parents=True)
        self.config_data = patch.object(config, "DATA_DIR", self.root)
        self.config_data.start()
        self.addCleanup(self.config_data.stop)
        self.agent = patch.object(config, "AGENT", "opencode")
        self.agent.start()
        self.addCleanup(self.agent.stop)
        self.env = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.xdg)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.writer = sqlite3.connect(self.db_path)
        self.addCleanup(self.writer.close)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("CREATE TABLE session(id TEXT PRIMARY KEY,parent_id TEXT,title TEXT,time_updated INTEGER)")
        self.writer.execute("CREATE TABLE todo(session_id TEXT,content TEXT,status TEXT,position INTEGER)")
        self.writer.execute("CREATE TABLE part(session_id TEXT,data TEXT,time_updated INTEGER)")
        self.writer.executemany("INSERT INTO session VALUES(?,?,?,?)", [
            ("ses-parent", None, "Implementar feature", 1000),
            ("ses-child", "ses-parent", "Cerrar proveedor API (@general subagent)", 3000)])
        self.writer.executemany("INSERT INTO todo VALUES(?,?,?,?)", [
            ("ses-parent", "Completar proveedor API", "in_progress", 0),
            ("ses-parent", "Integrar selector frontend", "pending", 1)])
        self.writer.executemany("INSERT INTO part VALUES(?,?,?)", [
            ("ses-parent", json.dumps({"type": "reasoning", "text": "PRIVATE REASONING"}), 1000),
            ("ses-child", json.dumps({"type": "tool", "tool": "bash", "state": {
                "status": "completed", "output": "PRIVATE OUTPUT", "input": {"prompt": "SECRET"}}}), 3000)])
        self.writer.commit()

    def test_reads_parent_todos_and_child_without_exposing_reasoning_or_tool_output(self):
        state = {"branch": "bot-feature", "session_id": "ses-parent"}
        result = activity.snapshot(state, {}, 5.0)
        self.assertEqual(result["todos"], {"in_progress": 1, "pending": 1})
        self.assertEqual(result["current_task"], "Completar proveedor API")
        self.assertEqual(result["children"][0]["title"], "Cerrar proveedor API")
        self.assertEqual(result["last_activity"], 3.0)
        self.assertIn("Cerrar proveedor API", result["activity"])
        self.assertNotIn("PRIVATE", str(result))
        self.assertNotIn("SECRET", str(result))
        self.assertFalse((self.root / "agent_activity.sqlite").exists())  # reader is read-only

    def test_new_wal_commit_becomes_visible_without_restarting_watcher(self):
        state = {"branch": "bot-feature", "session_id": "ses-parent"}
        before = activity.snapshot(state, {}, 5.0)
        self.writer.execute("INSERT INTO part VALUES(?,?,?)", ("ses-child", json.dumps({
            "type": "step-finish"}), 9000))
        self.writer.commit()
        after = activity.snapshot(state, {}, 10.0)
        self.assertGreater(after["last_activity"], before["last_activity"])
        self.assertEqual(after["last_activity"], 9.0)

    def test_schema_mismatch_gracefully_uses_durable_fallback(self):
        self.writer.execute("DROP TABLE todo")
        self.writer.commit()
        state = {"branch": "bot-feature", "session_id": "ses-parent", "state": "IMPLEMENTING"}
        activity.begin(state, "ses-parent")
        activity.observe(state, activity="tool: test", now=3.0)
        result = activity.snapshot(state, {}, 5.0)
        self.assertEqual(result["activity"], "tool: test")
        self.assertEqual(result["last_activity"], 3.0)
