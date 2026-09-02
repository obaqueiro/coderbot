"""Integration test for the bridge's OpenCode config hook."""
import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
PLUGIN_ROOT = ROOT / "agent-plugin"


class BridgeOpenCodePluginTests(unittest.TestCase):
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
