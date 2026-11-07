import json,tempfile,unittest
from pathlib import Path
from cli.app import dispatch
class Acceptance(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
 def tearDown(self):self.tmp.cleanup()
 def invoke(self,*extra,env=None):return dispatch(["manifest",*extra,"--state",str(self.root/"state.json")],env=env or {},cwd=self.root)
 def test_precedence(self):
  (self.root/"repo-cli.ini").write_text('[manifest]\nentries = 3\n')
  self.assertEqual(self.invoke("--entries",str(1),env={"REPOCLI_MANIFEST_ENTRIES":str(2)})[1]["entries"],1)
  self.assertEqual(self.invoke(env={"REPOCLI_MANIFEST_ENTRIES":str(2)})[1]["entries"],2)
 def test_invalid_values_are_rejected_without_side_effect(self):
  for bad in ("0",str(11),"1.5","abc"," 2 ","２"):
   before=(self.root/"state.json").read_bytes() if (self.root/"state.json").exists() else None
   code,result=self.invoke("--entries",bad)
   self.assertEqual(code,2,bad);self.assertEqual(result,{"error":{"code":"invalid_parameter","field":"entries"}})
   after=(self.root/"state.json").read_bytes() if (self.root/"state.json").exists() else None
   self.assertEqual(after,before)
