"""Run the whole ChordForge test suite with a clear summary. Usage: python3 -m tests.run_all"""
import sys
import time
import unittest

MODULES = ["tests.test_audio", "tests.test_mixing", "tests.test_data_layer", "tests.test_api",
           "tests.test_song", "tests.test_song_services", "tests.test_song_api"]


def main():
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for m in MODULES:
        suite.addTests(loader.loadTestsFromName(m))
    t0 = time.time()
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print(f"\n{result.testsRun} tests in {time.time() - t0:.1f}s | "
          f"failures={len(result.failures)} errors={len(result.errors)} skipped={len(result.skipped)}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
