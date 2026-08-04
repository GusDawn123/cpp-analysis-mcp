"""Put tests/ on sys.path so test modules can `import helpers`.

tests/ is not a package (no __init__.py), and pytest only prepends the directory
holding each test file, which is tests/unit/.
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = str(Path(__file__).resolve().parent)

if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)
