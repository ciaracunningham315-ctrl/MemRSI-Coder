import json, tempfile, unittest
from pathlib import Path
from cli.main import dispatch
class Acceptance(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); self.home=self.root/"home"; self.home.mkdir()
        (self.home/".config/repo-cli").mkdir(parents=True)
        (self.home/".config/repo-cli/config.json").write_text('{"archive":{"threads":12}}')
    def tearDown(self): self.tmp.cleanup()
    def invoke(self,*args,env=None): return dispatch(["archive",*args,"--state",str(self.root/"state.json")],env={"HOME":str(self.home),**(env or {})},cwd=self.root)
    def test_each_source_precedence_and_default(self):
        (self.root/".repo-cli.json").write_text('{"archive":{"threads":9}}')
        env={"REPOCLI_ARCHIVE_THREADS":"7","HOME":str(self.home)}
        self.assertEqual(self.invoke("--threads","3",env=env)[1]["threads"],3)
        self.assertEqual(self.invoke(env=env)[1]["threads"],7)
        self.assertEqual(self.invoke(env={"HOME":str(self.home)})[1]["threads"],9)
        (self.root/".repo-cli.json").unlink()
        self.assertEqual(self.invoke(env={"HOME":str(self.home)})[1]["threads"],12)
        (self.home/".config/repo-cli/config.json").unlink()
        self.assertEqual(self.invoke(env={})[1]["threads"],3)
    def test_boundaries_and_no_fallback_for_invalid_high_source(self):
        for valid in ("1","16"): self.assertEqual(self.invoke("--threads",valid)[0],0)
        for source,value,extra in (("cli","0",{}),("env","17",{"REPOCLI_ARCHIVE_THREADS":"17"})):
            before=(self.root/"state.json").read_bytes()
            result=self.invoke("--threads",value,env=extra) if source=="cli" else self.invoke(env={**extra,"HOME":str(self.home)})
            self.assertEqual(result[0],2); self.assertEqual(result[1],{"error":{"code":"invalid_parameter","field":"threads"}})
            self.assertEqual((self.root/"state.json").read_bytes(),before)
