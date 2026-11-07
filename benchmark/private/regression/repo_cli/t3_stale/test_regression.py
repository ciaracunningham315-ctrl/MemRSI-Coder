import tempfile, unittest
from pathlib import Path
from cli.main import dispatch
class Regression(unittest.TestCase):
    def test_health_and_prior_command_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dispatch(["health"],env={},cwd=tmp),(0,{"status":"ok"}))
            for command,field in (("publish","retries"),("mirror","workers")):
                result=dispatch([command,"--state",str(Path(tmp)/(command+".json"))],env={"HOME":tmp},cwd=tmp)
                self.assertEqual(result[0],0); self.assertEqual(result[1][field],2)
