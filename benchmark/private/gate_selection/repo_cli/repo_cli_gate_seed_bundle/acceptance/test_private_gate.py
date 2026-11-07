import json,tempfile,unittest
from pathlib import Path
from cli.app import dispatch
class Acceptance(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
 def tearDown(self):self.tmp.cleanup()
 def invoke(self,*extra,env=None):return dispatch(["bundle",*extra,"--state",str(self.root/"state.json")],env=env or {},cwd=self.root)
 def test_precedence(self):
  (self.root/".repocli.json").write_text('{"bundle": {"jobs": 3}}')
  self.assertEqual(self.invoke("--workers",str(1),env={"REPOCLI_BUNDLE_WORKERS":str(2)})[1]["workers"],1)
  self.assertEqual(self.invoke(env={"REPOCLI_BUNDLE_WORKERS":str(2)})[1]["workers"],2)
 def test_invalid_values_are_rejected_without_side_effect(self):
  for bad in ("0",str(7),"1.5","abc"," 2 ","２"):
   before=(self.root/"state.json").read_bytes() if (self.root/"state.json").exists() else None
   code,result=self.invoke("--workers",bad)
   self.assertEqual(code,2,bad);self.assertEqual(result,{"error":{"code":"invalid_parameter","field":"workers"}})
   after=(self.root/"state.json").read_bytes() if (self.root/"state.json").exists() else None
   self.assertEqual(after,before)
