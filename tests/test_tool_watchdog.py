"""Tool execution deadlines must not charge time spent pending."""
import os
import json
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import operations

ROOT = Path(__file__).resolve().parent.parent


class ToolTimeoutDefaults(unittest.TestCase):
    def test_default_is_200_seconds_and_explicit_overrides_are_preserved(self):
        from scripts.setup_catalog import BY_KEY
        self.assertEqual(BY_KEY["CODEBOT_LOCAL_TOOL_TIMEOUT"].default, "200")
        for override, expected in ((None, 200), ("75", 75)):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as directory:
                env = {"CODEBOT_DATA_DIR": directory}
                if override is not None:
                    env["CODEBOT_LOCAL_TOOL_TIMEOUT"] = override
                with patch.dict(os.environ, env, clear=True):
                    values = runpy.run_path(str(ROOT / "src/config.py"))
                self.assertEqual(values["LOCAL_TOOL_TIMEOUT_SECONDS"], expected)


class ToolWatchdogState(unittest.TestCase):
    def setUp(self):
        operations.clear()
        self.addCleanup(operations.clear)
        self.now = 100.0
        clock = patch.object(operations.time, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        timeout = patch.object(operations.config, "LOCAL_TOOL_TIMEOUT_SECONDS", 200)
        timeout.start()
        self.addCleanup(timeout.stop)

    def start(self, status, *, id="patch", tool="apply_patch", metadata=None):
        operations.observe({"type": "tool_start", "sourceSessionID": "root",
            "part": {"id": id, "tool": tool, "state": {"status": status, "metadata": metadata or {}}}})

    def test_pending_tools_do_not_start_deadlines(self):
        for tool in ("apply_patch", "read", "bash", "task"):
            self.start("pending", id=tool, tool=tool)
        self.now += 300
        self.assertEqual(operations.snapshot(), [])
        self.assertEqual(operations.expired(), [])

    def test_running_gets_full_budget_after_a_long_pending_period(self):
        self.start("pending")
        self.now += 300
        self.start("running")
        self.assertEqual(operations.snapshot()[0]["remaining"], 200)
        self.now += 199
        self.assertEqual(operations.expired(), [])
        self.now += 1
        self.assertEqual([entry["id"] for entry in operations.expired()], ["patch"])

    def test_duplicate_running_and_progress_do_not_extend_local_execution(self):
        self.start("running")
        self.now += 199
        self.start("running")
        operations.observe({"type": "step_finish", "sourceSessionID": "root"})
        self.assertEqual(operations.snapshot()[0]["remaining"], 1)
        self.now += 1
        self.assertEqual(len(operations.expired()), 1)

    def test_terminal_events_clear_deadlines_including_errors_without_running(self):
        for terminal in ("completed", "error"):
            with self.subTest(terminal=terminal):
                self.start("pending")
                if terminal == "completed":
                    self.start("running")
                operations.observe({"type": "tool_use", "part": {"id": "patch", "tool": "apply_patch",
                    "state": {"status": terminal}}})
                self.now += 300
                self.assertEqual(operations.expired(), [])
                self.assertEqual(operations.snapshot(), [])

    def test_unknown_or_terminal_start_states_are_not_execution(self):
        for status in (None, "unknown", "completed", "error"):
            self.start(status)
        self.assertEqual(operations.snapshot(), [])

    def test_pending_child_activity_does_not_refresh_subagent_idle_deadline(self):
        self.start("running", id="child", tool="task", metadata={"sessionId": "child"})
        self.now += 590
        operations.observe({"type": "tool_start", "sourceSessionID": "child",
            "part": {"id": "pending", "tool": "apply_patch", "state": {"status": "pending"}}})
        self.now += 10
        self.assertEqual([entry["id"] for entry in operations.expired()], ["child"])

    def test_running_metadata_and_child_progress_keep_subagent_tracking(self):
        self.start("running", id="task", tool="task")
        self.now += 100
        self.start("running", id="task", tool="task", metadata={"sessionId": "child"})
        self.now += 490
        operations.observe({"type": "step_finish", "sourceSessionID": "child"})
        self.assertEqual(operations.snapshot()[0]["remaining"], 600)
        self.assertEqual(operations.snapshot()[0]["session_id"], "child")

    def test_running_deadline_is_still_bounded_by_remaining_agent_budget(self):
        budget_class = operations.Budget
        with patch.object(operations, "Budget", side_effect=lambda seconds:
                          budget_class(seconds, clock=lambda: self.now)), operations.budget(210):
            self.start("pending")
            self.now += 200
            self.start("running")
            self.assertEqual(operations.snapshot()[0]["remaining"], 10)
            self.now += 10
            self.assertEqual(len(operations.expired()), 1)

    def test_shell_and_subagent_keep_their_distinct_running_limits(self):
        with patch.object(operations.config, "E2E_TIMEOUT_SECONDS", 900), \
             patch.object(operations.config, "SUBAGENT_TIMEOUT_SECONDS", 600):
            self.start("running", id="shell", tool="bash")
            self.start("running", id="child", tool="task")
        self.assertEqual({entry["id"]: entry["remaining"] for entry in operations.snapshot()},
                         {"shell": 900, "child": 600})

    def test_bridge_lifecycle_events_feed_the_real_watchdog(self):
        script = r"""
import {toolEvent} from './agent-plugin/runtime/protocol.js';
const started = new Set(), completed = new Set(), updates = [];
const part = {id:'patch', sessionID:'root', callID:'call', type:'tool', tool:'apply_patch'};
for (const [at, status] of [[0,'pending'], [300,'running'], [499,'running'],
                          [500,'completed'], [501,'completed'], [502,'running']]) {
  const event = toolEvent({...part, state:{status, output:'preserved result'}}, started, completed);
  updates.push({at, event});
}
console.log(JSON.stringify(updates));
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                                capture_output=True, text=True, timeout=10, check=True)
        updates = json.loads(result.stdout)
        self.assertEqual([update["at"] for update in updates if update["event"]], [300, 500])
        for update in updates:
            self.now = 100 + update["at"]
            if event := update["event"]:
                operations.observe({**event, "sourceSessionID": "root"})
            if update["at"] == 0:
                self.assertEqual(operations.snapshot(), [])
            elif update["at"] == 300:
                self.assertEqual(operations.snapshot()[0]["remaining"], 200)
            elif update["at"] == 499:
                self.assertEqual(operations.snapshot()[0]["remaining"], 1)
            else:
                self.assertEqual(operations.snapshot(), [])
        self.assertEqual(updates[3]["event"]["part"]["state"]["output"], "preserved result")

    def test_bridge_preserves_child_metadata_terminal_errors_and_fallback_ids(self):
        script = r"""
import assert from 'node:assert/strict';
import {toolEvent} from './agent-plugin/runtime/protocol.js';
const started = new Set(), completed = new Set();
const part = {sessionID:'child', callID:'call', type:'tool', tool:'task'};
const event = state => toolEvent({...part, state}, started, completed);
assert.equal(event({status:'pending'}), null);
assert.equal(started.size, 0);
assert.equal(event({status:'unknown'}), null);
const first = event({status:'running'});
assert.equal(first.type, 'tool_start');
assert.equal(first.part.id, 'child:call');
assert.equal(event({status:'running'}), null);
assert.equal(event({status:'running', metadata:{sessionId:'grandchild'}}).part.state.metadata.sessionId, 'grandchild');
assert.equal(event({status:'error', error:'delegation rejected'}).type, 'tool_use');
assert.equal(event({status:'error'}), null);
assert.equal(event({status:'running', metadata:{sessionId:'grandchild'}}), null);
const rejected = {id:'rejected', sessionID:'child', tool:'task', state:{status:'error', error:'rejected before running'}};
assert.equal(toolEvent(rejected, started, completed).part.state.error, 'rejected before running');
assert.equal(toolEvent(rejected, started, completed), null);
"""
        subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                       capture_output=True, text=True, timeout=10, check=True)
