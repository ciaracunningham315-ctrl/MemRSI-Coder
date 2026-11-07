import tempfile, unittest
from pathlib import Path
from cli.main import dispatch
class Regression(unittest.TestCase):
    def test_default_publish_and_health_remain_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/"state.json"
            self.assertEqual(dispatch(["health"], env={}, cwd=tmp), (0,{"status":"ok"}))
            code, record=dispatch(["publish","--state",str(state)], env={}, cwd=tmp)
            self.assertEqual(code,0); self.assertEqual(record["retries"],2); self.assertEqual(record["id"],1)
