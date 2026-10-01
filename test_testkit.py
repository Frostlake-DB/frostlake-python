"""Replays the engine's testkit corpus through the driver, by way of testkit_runner.py.

Runs only with FL_CORPUS naming frostlake's engine/src/test/resources/testkit, against an
engine named as for the integration tests (or FROSTLAKE_URL, for one already running):

    export FL_CORPUS=/path/to/frostlake/engine/src/test/resources/testkit
    FROSTLAKE_CLASSPATH=... python3 -m unittest -v
"""

import importlib.util
import os
import pathlib
import unittest

RUNNER = pathlib.Path(__file__).resolve().parent / "testkit_runner.py"


@unittest.skipUnless(os.environ.get("FL_CORPUS"),
                     "set FL_CORPUS to frostlake's engine/src/test/resources/testkit"
                     " to replay the testkit corpus")
class TestkitCorpusTest(unittest.TestCase):

    def test_corpus_replays_without_failures(self):
        spec = importlib.util.spec_from_file_location("testkit_runner", RUNNER)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        runner.suites_directory()   # an FL_CORPUS without suites fails here, before any engine
        if not (os.environ.get("FROSTLAKE_CLASSPATH") or os.environ.get("FROSTLAKE_URL")):
            self.skipTest("no engine (set FROSTLAKE_CLASSPATH or FROSTLAKE_URL)")
        # The runner prints its tally and first failures, and returns 1 on a failed or
        # errored case.
        self.assertEqual(0, runner.main(["--backend", "python"]))


if __name__ == "__main__":
    unittest.main()
