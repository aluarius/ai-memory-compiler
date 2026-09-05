from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for candidate in (ROOT / "scripts", ROOT / "hooks"):
    path_str = str(candidate)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)


@pytest.fixture(autouse=True)
def protect_workspace_memory(monkeypatch):
    """Tests must use temporary stores, never the developer's migrated corpus."""
    from memory_store import MemoryStore
    original = MemoryStore.__init__

    def isolated_init(self, root, **kwargs):
        if Path(root).resolve() == ROOT.resolve():
            raise AssertionError("Test attempted to access workspace memory; isolate ROOT_DIR")
        original(self, root, **kwargs)

    monkeypatch.setattr(MemoryStore, "__init__", isolated_init)
