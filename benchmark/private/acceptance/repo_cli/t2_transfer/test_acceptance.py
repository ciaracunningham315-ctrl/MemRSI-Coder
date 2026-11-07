import json, tempfile, unittest
from pathlib import Path
from cli.main import dispatch
class Acceptance(unittest.TestCase):
    def setUp(self): self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); (self.root/"repo-cli.ini").write_text("[mirror]\nworkers = 6\n")
    def tearDown(self): self.tmp.cleanup()
    def invoke(self,*args,env=None): return dispatch(["mirror",*args,"--state",str(self.root/"state.json")],env=env or {},cwd=self.root)
    def test_source_order_cli_environment_ini_default(self):
        self.assertEqual(self.invoke("--workers","2",env={"REPOCLI_MIRROR_WORKERS":"4"})[1]["workers"],2)
        self.assertEqual(self.invoke(env={"REPOCLI_MIRROR_WORKERS":"4"})[1]["workers"],4)
        (self.root/"repo-cli.ini").write_text("[mirror]\nworkers = 7\n")
        self.assertEqual(self.invoke()[1]["workers"],7)
    def test_range_and_invalid_type_do_not_consume_identifier(self):
        for valid in ("1","8"): self.assertEqual(self.invoke("--workers",valid)[0],0)
        for invalid in ("0","9","3.5","one"," 2 ","３"):
            before=(self.root/"state.json").read_bytes()
            code,result=self.invoke("--workers",invalid)
            self.assertEqual(code,2,invalid); self.assertEqual(result,{"error":{"code":"invalid_parameter","field":"workers"}})
            self.assertEqual((self.root/"state.json").read_bytes(),before)
