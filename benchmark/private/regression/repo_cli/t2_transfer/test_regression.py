import tempfile, unittest, json
from pathlib import Path
from cli.main import dispatch
class Regression(unittest.TestCase):
    def test_seed_publish_defaults_and_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/"state.json")
            self.assertEqual(dispatch(["health"],env={},cwd=tmp),(0,{"status":"ok"}))
            self.assertEqual(dispatch(["publish","--state",path],env={},cwd=tmp)[1]["retries"],2)
            state=json.loads(Path(path).read_text()); self.assertEqual(state["next_id"],2)
    def test_mirror_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=dispatch(["mirror","--state",str(Path(tmp)/"s.json")],env={},cwd=tmp)
            self.assertEqual((result[0],result[1]["workers"]),(0,2))
