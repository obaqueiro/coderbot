from pathlib import Path
import tempfile
import unittest
from package_registry import public_runtime_npmrc


class RegistryTests(unittest.TestCase):
    def test_public_registry_keeps_scope_and_auth_and_original_file(self):
        with tempfile.TemporaryDirectory() as root:
            source, target = Path(root) / "source", Path(root) / "runtime"
            original = "registry=https://private/\n@example:registry=https://private/\n//private/:_authToken=fixture\n"
            source.write_text(original)
            public_runtime_npmrc(source, target)
            self.assertEqual(source.read_text(), original)
            self.assertIn("@example:registry=https://private/", target.read_text())
            self.assertIn("//private/:_authToken=fixture", target.read_text())
            self.assertIn("registry=https://registry.npmjs.org/", target.read_text())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
