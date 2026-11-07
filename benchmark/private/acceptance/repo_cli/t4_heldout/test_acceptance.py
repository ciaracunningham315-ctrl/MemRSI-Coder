import json, tempfile, unittest
from pathlib import Path
from cli.main import dispatch
class Acceptance(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); self.home=self.root/"home"; self.home.mkdir(); (self.home/".config/repo-cli").mkdir(parents=True)
        self.global_path=self.home/".config/repo-cli/config.json"; self.state=self.root/"state.json"
    def tearDown(self): self.tmp.cleanup()
    def invoke(self,*args,env=None): return dispatch(["deploy",*args,"--state",str(self.state)],env={"HOME":str(self.home),**(env or {})},cwd=self.root)
    def test_per_option_mixed_sources_and_override_isolation(self):
        (self.root/".repo-cli.json").write_text('{"deploy":{"workers":8,"timeout":80}}')
        self.global_path.write_text('{"deploy":{"workers":9,"timeout":90}}')
        env={"REPOCLI_DEPLOY_WORKERS":"6","REPOCLI_DEPLOY_TIMEOUT":"60","HOME":str(self.home)}
        result=self.invoke("--workers","4",env=env)
        self.assertEqual(result[0],0); self.assertEqual((result[1]["workers"],result[1]["timeout"]),(4,60))
        result=self.invoke(env={"REPOCLI_DEPLOY_TIMEOUT":"45","HOME":str(self.home)})
        self.assertEqual((result[1]["workers"],result[1]["timeout"]),(8,45))
        result=self.invoke(env={"HOME":str(self.home)})
        self.assertEqual((result[1]["workers"],result[1]["timeout"]),(8,80))
    def test_boundaries_invalid_selected_values_and_atomicity(self):
        for args in (("--workers","1","--timeout","5"),("--workers","16","--timeout","120")):
            self.assertEqual(self.invoke(*args)[0],0)
        for args in (("--workers","0","--timeout","30"),("--workers","2","--timeout","121"),("--workers","２","--timeout","30")):
            before=self.state.read_bytes(); result=self.invoke(*args)
            self.assertEqual(result[0],2); self.assertIn(result[1]["error"]["field"],("workers","timeout"))
            self.assertEqual(self.state.read_bytes(),before)
        self.global_path.write_text('{"deploy":{"workers":4,"timeout":30}}')
        before=self.state.read_bytes()
        result=self.invoke(env={"REPOCLI_DEPLOY_WORKERS":"0","HOME":str(self.home)})
        self.assertEqual(result[0],2); self.assertEqual(result[1]["error"]["field"],"workers")
        self.assertEqual(self.state.read_bytes(),before)
