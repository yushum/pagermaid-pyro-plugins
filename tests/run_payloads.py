import sys
import unittest
from pathlib import Path

suite = unittest.defaultTestLoader.discover(
    str(Path(__file__).parent), pattern="test_linkcopy_payloads.py",
)
result = unittest.TextTestRunner(stream=sys.__stdout__, verbosity=2).run(suite)
if result.testsRun == 0:
    raise RuntimeError("没有执行任何测试。")
sys.exit(0 if result.wasSuccessful() else 1)
