import json,tempfile,unittest
from pathlib import Path
from cli.app import dispatch
class Acceptance(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
 def tearDown(self):self.tmp.cleanup()
 def invoke(self,*extra,env=None):return dispatch(["release",*extra,"--state",str(self.root/"state.json")],env=env or {},cwd=self.root)
 def test_precedence(self):
  (self.root/".repo-cli.json").write_text('{"release": {"parallel": 3}}')
  self.assertEqual(self.invoke("--parallel",str(1),env={"REPOCLI_RELEASE_PARALLEL":str(2)})[1]["parallel"],1)
  self.assertEqual(self.invoke(env={"REPOCLI_RELEASE_PARALLEL":str(2)})[1]["parallel"],2)
 def test_invalid_values_are_rejected_without_side_effect(self):
  for bad in ("0",str(13),"1.5","abc"," 2 ","２"):
   before=(self.root/"state.json").read_bytes() if (self.root/"state.json").exists() else None
   code,result=self.invoke("--parallel",bad)
   self.assertEqual(code,2,bad);self.assertEqual(result,{"error":{"code":"invalid_parameter","field":"parallel"}})
   after=(self.root/"state.json").read_bytes() if (self.root/"state.json").exists() else None
   self.assertEqual(after,before)
