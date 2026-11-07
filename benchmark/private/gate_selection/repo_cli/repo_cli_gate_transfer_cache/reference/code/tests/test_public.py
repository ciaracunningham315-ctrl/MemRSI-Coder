import tempfile,unittest
from pathlib import Path
from cli.app import dispatch
class Public(unittest.TestCase):
 def test_health_and_default(self):
  with tempfile.TemporaryDirectory() as d:
   self.assertEqual(dispatch(["health"],env={},cwd=d),(0,{"status":"ok"}))
   code,row=dispatch(['cache',"--state",str(Path(d)/"s.json")],env={},cwd=d)
   self.assertEqual(code,0);self.assertEqual(row["id"],1)
