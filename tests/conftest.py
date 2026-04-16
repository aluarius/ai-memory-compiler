from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for candidate in (ROOT / "scripts", ROOT / "hooks"):
    path_str = str(candidate)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
