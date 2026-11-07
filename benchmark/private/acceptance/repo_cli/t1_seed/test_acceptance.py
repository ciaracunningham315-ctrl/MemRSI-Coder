import json, tempfile, unittest
from pathlib import Path
from cli.main import dispatch

class Acceptance(unittest.TestCase):
    def setUp(self): self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def invoke(self, *args, env=None): return dispatch(["publish", *args, "--state", str(self.root / "state.json")], env=env or {}, cwd=self.root)
    def test_precedence_is_cli_then_env_then_project(self):
        (self.root / ".repocli.json").write_text('{"publish":{"retries":5}}')
        self.assertEqual(self.invoke("--retries", "1", env={"REPOCLI_PUBLISH_RETRIES":"3"})[1]["retries"], 1)
        self.assertEqual(self.invoke(env={"REPOCLI_PUBLISH_RETRIES":"3"})[1]["retries"], 3)
        (self.root / ".repocli.json").write_text('{"publish":{"retries":4}}')
        self.assertEqual(self.invoke()[1]["retries"], 4)
    def test_ascii_integer_boundaries_and_rejected_values_are_atomic(self):
        for valid in ("1", "5"):
            code, result = self.invoke("--retries", valid)
            self.assertEqual(code, 0, result)
        for invalid in ("0", "6", "1.0", "two", " 2 ", "２"):
            before = (self.root / "state.json").read_bytes()
            code, result = self.invoke("--retries", invalid)
            self.assertEqual(code, 2, invalid)
            self.assertEqual(result, {"error":{"code":"invalid_parameter","field":"retries"}})
            self.assertEqual((self.root / "state.json").read_bytes(), before)
