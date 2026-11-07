import tempfile, unittest
from pathlib import Path
from cli.main import dispatch
class Regression(unittest.TestCase):
    def test_health_and_prior_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dispatch(["health"],env={},cwd=tmp),(0,{"status":"ok"}))
            for command,field in (("publish","retries"),("mirror","workers"),("archive","threads")):
                result=dispatch([command,"--state",str(Path(tmp)/(command+".json"))],env={"HOME":tmp},cwd=tmp)
                self.assertEqual(result[0],0); self.assertIn(field,result[1])
    def test_default_deploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=dispatch(["deploy","--state",str(Path(tmp)/"d.json")],env={"HOME":tmp},cwd=tmp)
            self.assertEqual(result[0],0); self.assertEqual((result[1]["workers"],result[1]["timeout"]),(3,30))
