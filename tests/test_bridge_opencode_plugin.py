"""Integration test for the bridge's OpenCode config hook."""
import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = ROOT / "agent-plugin"


class BridgeOpenCodePluginTests(unittest.TestCase):
    def run_script(self, script):
        return subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                              capture_output=True, text=True, timeout=10, check=True)

    def test_nested_delegation_is_rejected_before_queueing_including_resumed_children(self):
        self.run_script(r"""
import assert from 'node:assert/strict';
import {CoderbotOpenSpecPlugin} from './agent-plugin/.opencode/plugins/coderbot-openspec.js';
process.env.CODEBOT_MAX_SUBAGENTS = '1';
const hook = await CoderbotOpenSpecPlugin({serverUrl: 'http://localhost', directory: '/scratch',
 fetch: async url => ({ok: true, json: async () => ({parentID: url.pathname.endsWith('/resumed') ? 'root' : undefined})})});
await hook.event({event: {type: 'session.created', properties: {info: {id: 'child', parentID: 'root'}}}});
await hook['tool.execute.before']({tool:'task', callID:'parent', sessionID:'root'}, {});
for (const sessionID of ['child', 'resumed']) {
  await assert.rejects(hook['tool.execute.before']({tool:'task', callID:sessionID, sessionID}, {}),
    /Nested subagent delegation is disabled/);
}
// Normal tools remain available to children.
await hook['tool.execute.before']({tool:'read', callID:'read', sessionID:'child'}, {});
await hook['tool.execute.after']({callID:'parent'});
await hook['tool.execute.before']({tool:'task', callID:'next', sessionID:'root'}, {});
await hook['tool.execute.after']({callID:'next'});
""")

    def test_failed_task_releases_slot_once_and_preserves_fifo_reservations(self):
        self.run_script(r"""
import assert from 'node:assert/strict';
import {CoderbotOpenSpecPlugin} from './agent-plugin/.opencode/plugins/coderbot-openspec.js';
process.env.CODEBOT_MAX_SUBAGENTS = '1';
const hook = await CoderbotOpenSpecPlugin({serverUrl: 'http://localhost', directory: '/scratch',
 fetch: async () => ({ok: true, json: async () => ({id: 'root'})})});
const start = callID => hook['tool.execute.before']({tool:'task', callID, sessionID:'root'}, {});
await start('first');
const order = [];
const second = start('second').then(() => order.push('second'));
await new Promise(resolve => setTimeout(resolve, 10));
await hook.event({event: {type:'message.part.updated', properties: {part: {
 type:'tool', callID:'first', state:{status:'error'}}}}});
const third = start('third').then(() => order.push('third'));
await second;
await hook['tool.execute.after']({callID:'first'}); // duplicate completion must not free another slot
await new Promise(resolve => setTimeout(resolve, 10));
assert.deepEqual(order, ['second']);
await hook['tool.execute.after']({callID:'second'});
await third;
assert.deepEqual(order, ['second', 'third']);
await hook['tool.execute.after']({callID:'third'});
""")

    def test_queue_timeout_removes_waiter_without_leaking_a_slot(self):
        self.run_script(r"""
import assert from 'node:assert/strict';
import {CoderbotOpenSpecPlugin} from './agent-plugin/.opencode/plugins/coderbot-openspec.js';
process.env.CODEBOT_MAX_SUBAGENTS = '1';
process.env.CODEBOT_LOCAL_TOOL_TIMEOUT = '0.02';
const hook = await CoderbotOpenSpecPlugin({serverUrl: 'http://localhost', directory: '/scratch',
 fetch: async () => ({ok: true, json: async () => ({id: 'root'})})});
const start = callID => hook['tool.execute.before']({tool:'task', callID, sessionID:'root'}, {});
await start('first');
await assert.rejects(start('expired'), /Subagent queue timeout/);
await hook['tool.execute.after']({callID:'expired'});
await assert.rejects(start('still-full'), /Subagent queue timeout/);
await hook['tool.execute.after']({callID:'first'});
await start('next');
await hook['tool.execute.after']({callID:'next'});
""")

    def test_queue_timeout_defaults_to_200_seconds_and_preserves_overrides(self):
        self.run_script(r"""
import assert from 'node:assert/strict';
import {CoderbotOpenSpecPlugin} from './agent-plugin/.opencode/plugins/coderbot-openspec.js';
process.env.CODEBOT_MAX_SUBAGENTS = '1';
for (const [override, seconds] of [[undefined, 200], ['75', 75]]) {
  if (override === undefined) delete process.env.CODEBOT_LOCAL_TOOL_TIMEOUT;
  else process.env.CODEBOT_LOCAL_TOOL_TIMEOUT = override;
  const hook = await CoderbotOpenSpecPlugin({serverUrl:'http://localhost', directory:'/scratch',
    fetch:async () => ({ok:true, json:async () => ({id:'root'})})});
  await hook['tool.execute.before']({tool:'task', callID:'first', sessionID:'root'}, {});
  const original = globalThis.setTimeout;
  let observed;
  globalThis.setTimeout = (fn, ms) => {observed = ms; return original(fn, 0);};
  try {
    await assert.rejects(hook['tool.execute.before']({tool:'task', callID:'queued', sessionID:'root'}, {}),
      /Subagent queue timeout/);
  } finally {
    globalThis.setTimeout = original;
    await hook['tool.execute.after']({callID:'first'});
  }
  assert.equal(observed, seconds * 1000);
}
""")

    def test_invalid_concurrency_is_rejected(self):
        self.run_script(r"""
import assert from 'node:assert/strict';
import {CoderbotOpenSpecPlugin} from './agent-plugin/.opencode/plugins/coderbot-openspec.js';
for (const limit of ['0', '-1', 'NaN', '1.5']) {
 process.env.CODEBOT_MAX_SUBAGENTS = limit;
 await assert.rejects(CoderbotOpenSpecPlugin({}), /must be positive/);
}
""")

    def test_session_lookup_failure_does_not_reserve_a_slot(self):
        self.run_script(r"""
import assert from 'node:assert/strict';
import {CoderbotOpenSpecPlugin} from './agent-plugin/.opencode/plugins/coderbot-openspec.js';
process.env.CODEBOT_MAX_SUBAGENTS = '1';
let fail = true;
const hook = await CoderbotOpenSpecPlugin({serverUrl: 'http://localhost', directory: '/scratch',
 fetch: async (_, options) => ({ok: options.method !== 'GET' || !fail,
 status: 503, json: async () => ({id:'root'})})});
const start = callID => hook['tool.execute.before']({tool:'task', callID, sessionID:'root'}, {});
await assert.rejects(start('failed'), /Trusted runtime request failed/);
fail = false;
await start('next');
await hook['tool.execute.after']({callID:'next'});
""")

    def test_subagent_limit_queues_until_assignment_finishes(self):
        script = r"""
import {CoderbotOpenSpecPlugin} from './agent-plugin/.opencode/plugins/coderbot-openspec.js';
process.env.CODEBOT_MAX_SUBAGENTS = '1';
const hook = await CoderbotOpenSpecPlugin({serverUrl: 'http://localhost', directory: '/scratch',
 fetch: async () => ({ok: true, json: async () => ({id: 'a'})})});
await hook['tool.execute.before']({tool:'task',callID:'1',sessionID:'a'}, {});
let started = false;
const queued = hook['tool.execute.before']({tool:'task',callID:'2',sessionID:'a'}, {}).then(() => started=true);
await new Promise(resolve => setTimeout(resolve, 10));
if (started) throw new Error('limit ignored');
await hook['tool.execute.after']({callID:'1'});
await queued;
console.log(started);
"""
        proc = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                              capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(proc.stdout.strip(), "true")
    def test_trusted_hook_overrides_inherited_agent_restrictions(self):
        script = r"""
import {CoderbotOpenSpecPlugin} from './agent-plugin/.opencode/plugins/coderbot-openspec.js';
const hook = await CoderbotOpenSpecPlugin({});
const config = {permission: {external_directory: 'ask'}, agent: {
  explore: {permission: {edit: 'deny'}, description: 'retain'},
  custom: {permission: 'deny', model: 'provider/model'}
}};
await hook.config(config);
console.log(JSON.stringify(config));
"""
        proc = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                              capture_output=True, text=True, timeout=10, check=True)
        config = json.loads(proc.stdout)
        self.assertEqual(config["permission"], {"*": "allow"})
        self.assertTrue(all(a["permission"] == {"*": "allow"} for a in config["agent"].values()))
        self.assertEqual(config["agent"]["explore"]["description"], "retain")
        self.assertEqual(config["agent"]["custom"]["model"], "provider/model")

    def test_package_hook_additively_registers_bridge_skills(self):
        script = r"""
import fs from 'fs';
import path from 'path';
import { pathToFileURL } from 'url';

const root = process.argv[1];
const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
const plugin = await import(pathToFileURL(path.join(root, pkg.main)).href);
const hook = await plugin.CoderbotOpenSpecPlugin({});
const config = { skills: { paths: ['/target/project-skills'] }, untouched: true };
await hook.config(config);
await hook.config(config);
process.stdout.write(JSON.stringify(config));
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script, str(PLUGIN_ROOT)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        config = json.loads(proc.stdout)
        self.assertIs(config["untouched"], True)
        self.assertEqual(config["skills"]["paths"][0], "/target/project-skills")
        self.assertEqual(config["skills"]["paths"].count(
            str((PLUGIN_ROOT / "skills").resolve())), 1)


if __name__ == "__main__":
    unittest.main()
