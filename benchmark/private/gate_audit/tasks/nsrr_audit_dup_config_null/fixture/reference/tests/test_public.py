import unittest

import task_module

class PublicSurfaceTests(unittest.TestCase):
    def test_run_is_callable(self):
        self.assertTrue(callable(task_module.run))

if __name__ == "__main__":
    unittest.main()
