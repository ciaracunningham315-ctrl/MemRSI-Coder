import tempfile
import unittest
from pathlib import Path
from cli.main import dispatch

class PublicCLITests(unittest.TestCase):
    def test_health(self):
        code, result = dispatch(["health"], env={}, cwd=Path.cwd())
        self.assertEqual((code, result), (0, {"status": "ok"}))

    def test_default_command_writes_one_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = 'mirror'
            field = 'workers'
            code, result = dispatch([command, "--state", str(Path(tmp) / "state.json")], env={}, cwd=tmp)
            self.assertEqual(code, 0)
            self.assertEqual(result["id"], 1)
            self.assertEqual(result["command"], command)
            self.assertIn(field, result)
